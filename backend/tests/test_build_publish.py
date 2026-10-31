"""Publishing the first build as a pull request: the zip is read as untrusted, the default branch is never touched, and only an allowed card gets here."""
import asyncio
import json
import subprocess
import zipfile

import httpx
import pytest

from core.access import hash_password
from core.models import Agent, DocumentRecord, Project, ProjectMember, User
from core.usage import reset_usage_user, set_usage_user
from services import build_publish as bpub, code_review as cr

REAL_CLIENT = httpx.AsyncClient


def run(coro):
    return asyncio.run(coro)


def make_zip(path, entries):
    with zipfile.ZipFile(path, "w") as zf:
        for name, data in entries.items():
            if isinstance(data, zipfile.ZipInfo):
                zf.writestr(data, "target")
            else:
                zf.writestr(name, data)
    return path


# ── reading the archive as untrusted ─────────────────────────────────

def test_a_zip_is_unpacked_without_leaving_its_folder_or_touching_git(tmp_path):
    link = zipfile.ZipInfo("evil-link")
    link.external_attr = (0o120777 << 16)  # a symbolic link
    archive = make_zip(tmp_path / "a.zip", {
        "app.py": "print(1)", "src/util.py": "x", "../escape.txt": "no", "/abs.txt": "no", "sub/../../up.txt": "no", ".git/config": "no", "nested/.git/hooks/x": "no",
        "evil-link": link, "folder/": ""})
    dest = tmp_path / "out"
    dest.mkdir()
    assert bpub.safe_extract(archive, dest) == 2
    assert sorted(str(p.relative_to(dest)) for p in dest.rglob("*") if p.is_file()) == ["app.py", "src/util.py"]
    assert not (tmp_path / "escape.txt").exists() and not (tmp_path / "up.txt").exists() and not (dest / "evil-link").exists()


def test_a_zip_that_is_too_large_or_has_too_many_files_is_refused(tmp_path, monkeypatch):
    archive = make_zip(tmp_path / "a.zip", {f"f{i}.txt": "x" for i in range(5)})
    dest = tmp_path / "out"
    dest.mkdir()
    monkeypatch.setattr(bpub, "MAX_FILES", 3)
    with pytest.raises(RuntimeError, match="too large"):
        bpub.safe_extract(archive, dest)
    monkeypatch.setattr(bpub, "MAX_FILES", 100)
    monkeypatch.setattr(bpub, "MAX_BYTES", 2)
    with pytest.raises(RuntimeError, match="too large"):
        bpub.safe_extract(archive, dest)
    assert list(dest.iterdir()) == []


# ── a real git remote ────────────────────────────────────────────────

def git(*args, cwd):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout


@pytest.fixture
def world(db, tmp_path, monkeypatch):
    monkeypatch.setattr("tools.documents.DOC_DIR", tmp_path / "docs")
    monkeypatch.setattr("tools.documents._r2", lambda: None)
    (tmp_path / "docs").mkdir()
    user = User(id="user_p", email="p@example.com", name="Pat", password_hash=hash_password("testpass123"), plan="free")
    proj = Project(id="proj_p", name="Lumen", repo="acme/lumen")
    other = Project(id="proj_q", name="Other")
    db.add_all([user, proj, other])
    db.commit()
    db.add(ProjectMember(project_id=proj.id, user_id=user.id, role="owner"))
    db.add(Agent(id="agent_p", name="Master", system_prompt="x", project_id=proj.id, is_master="true"))
    db.commit()

    bare = tmp_path / "origin.git"
    git("init", "-q", "--bare", "-b", "main", str(bare), cwd=tmp_path)
    seed = tmp_path / "seed"
    git("clone", "-q", str(bare), str(seed), cwd=tmp_path)
    (seed / "README.md").write_text("# Lumen\n")
    git("add", "-A", cwd=seed)
    git("-c", "user.name=Human", "-c", "user.email=h@example.com", "commit", "-q", "-m", "first", cwd=seed)
    git("push", "-q", "origin", "HEAD:main", cwd=seed)

    real = cr.git_run

    async def git_run(*args, cwd, token="", timeout=300):  # github.com is the local repository here
        return await real(*[str(bare) if a == "https://github.com/acme/lumen.git" else a for a in args], cwd=cwd, token=token, timeout=timeout)

    async def token(repo, uid):
        return "ghs_TOKEN"

    async def identity():
        return "neuron-bot", "neuron-bot[bot]@users.noreply.github.com"
    monkeypatch.setattr(cr, "git_run", git_run)
    monkeypatch.setattr(cr, "_token", token)
    monkeypatch.setattr("services.wiki._bot_identity", identity)

    sent = {"requests": [], "pr_status": 201}

    def handler(request):
        body = json.loads(request.content) if request.content else None
        sent["requests"].append((request.method, request.url.path, body, request.headers.get("authorization")))
        if request.method == "GET":
            return httpx.Response(200, json={"default_branch": "main"})
        return httpx.Response(sent["pr_status"], json={"html_url": "https://github.com/acme/lumen/pull/7"})
    monkeypatch.setattr(bpub.httpx, "AsyncClient", lambda **kw: REAL_CLIENT(transport=httpx.MockTransport(handler), **kw))

    token_ctx = set_usage_user(user.id, proj.id)
    yield user, proj, bare, sent
    reset_usage_user(token_ctx)


def save_archive(db, tmp_path, entries, name="lumen-first-build-20261029-100000.zip", project_id="proj_p", owner="user_p"):
    make_zip(tmp_path / "docs" / name, entries)
    db.add(DocumentRecord(filename=name, agent_id="agent_p", project_id=project_id, user_id=owner, created_by=owner, shared_with=[], title="Lumen: first build (code)"))
    db.commit()
    return name


def test_the_build_goes_to_a_branch_and_a_pull_request_is_opened(db, world, tmp_path):
    user, proj, bare, sent = world
    guide = (tmp_path / "docs" / "lumen-run-guide-20261029-100000.md")
    guide.write_text("# Run guide\n```sh\npython app.py\n```\n")
    name = save_archive(db, tmp_path, {"app.py": "print('hi')\n", "README.md": "# Lumen first build\n", "src/shift.py": "x = 1\n"})

    out = run(bpub.publish("acme/lumen", user.id, "agent_p", name, guide.name))

    assert out.startswith("Pull request opened: https://github.com/acme/lumen/pull/7 (3 files on branch neuron/first-build-")
    branches = [b.strip() for b in git("branch", "-a", cwd=bare).splitlines()]
    (branch,) = [b for b in branches if b.startswith("neuron/first-build-")]
    assert git("show", f"{branch}:app.py", cwd=bare) == "print('hi')\n" and git("show", f"{branch}:README.md", cwd=bare) == "# Lumen first build\n"
    assert git("show", "main:README.md", cwd=bare) == "# Lumen\n"  # the default branch is not touched
    assert git("log", branch, "-1", "--format=%an <%ae>|%s", cwd=bare).strip() == "neuron-bot <neuron-bot[bot]@users.noreply.github.com>|Add the first build of Lumen"
    method, path, body, auth = sent["requests"][-1]
    assert (method, path, auth) == ("POST", "/repos/acme/lumen/pulls", "Bearer ghs_TOKEN")
    assert body["head"] == branch and body["base"] == "main" and body["title"] == "First build of Lumen" and "python app.py" in body["body"] and "Muse" in body["body"]


def test_publishing_the_same_code_again_opens_nothing(db, world, tmp_path):
    user, proj, bare, sent = world
    name = save_archive(db, tmp_path, {"README.md": "# Lumen\n"})  # what the repository already has
    assert run(bpub.publish("acme/lumen", user.id, "agent_p", name)) == "Nothing to publish: acme/lumen already has this code."
    assert not [r for r in sent["requests"] if r[0] == "POST"] and "neuron/first-build" not in git("branch", "-a", cwd=bare)


def test_a_pull_request_github_refuses_still_leaves_the_pushed_branch_and_says_so(db, world, tmp_path):
    user, proj, bare, sent = world
    sent["pr_status"] = 422
    name = save_archive(db, tmp_path, {"app.py": "1\n"})
    with pytest.raises(RuntimeError, match=r"The branch neuron/first-build-\d{8}-\d{6} was pushed, but GitHub would not open the pull request \(422\)"):
        run(bpub.publish("acme/lumen", user.id, "agent_p", name))
    assert "neuron/first-build" in git("branch", "-a", cwd=bare)


def test_only_this_projects_readable_archive_can_be_published(db, world, tmp_path):
    user, proj, bare, sent = world
    foreign = save_archive(db, tmp_path, {"a.py": "1"}, name="other-first-build-20261029-100000.zip", project_id="proj_q")
    private = save_archive(db, tmp_path, {"a.py": "1"}, name="private-first-build-20261029-100000.zip", owner="someone_else")
    not_a_zip = "plan-20261029-100000.md"
    (tmp_path / "docs" / not_a_zip).write_text("x")
    db.add(DocumentRecord(filename=not_a_zip, agent_id="agent_p", project_id="proj_p", user_id="user_p", created_by="user_p", shared_with=[], title="x"))
    db.commit()
    for name in (foreign, private, not_a_zip, "missing-20261029-100000.zip", "../../etc/passwd"):
        with pytest.raises(ValueError, match="could not be found"):
            run(bpub.publish("acme/lumen", user.id, "agent_p", name))
    assert sent["requests"] == []  # GitHub was never asked


def test_a_project_without_a_repository_has_nothing_to_publish_to(db, world, tmp_path):
    user, proj, bare, sent = world
    with pytest.raises(ValueError, match="no repository"):
        run(bpub.publish("", user.id, "agent_p", "x.zip"))


# ── how it is reached ────────────────────────────────────────────────

def test_the_dispatcher_runs_it_as_the_person_who_allowed_it_and_turns_failures_into_text(monkeypatch):
    from tools import dispatch
    seen = []

    async def fake(repo, uid, agent, archive, guide=""):
        seen.append((repo, uid, agent, archive, guide))
        if archive == "boom.zip":
            raise RuntimeError("git push failed")
        return "Pull request opened: https://example/pr/1"
    monkeypatch.setattr(bpub, "publish", fake)
    token = set_usage_user("user_p", "proj_p")
    try:
        ok = run(dispatch.run_tool("publish_build", {"archive": "a.zip", "guide": "g.md"}, "", "acme/lumen", "agent_p"))
        bad = run(dispatch.run_tool("publish_build", {"archive": "boom.zip"}, "", "acme/lumen", "agent_p"))
    finally:
        reset_usage_user(token)
    assert ok == "Pull request opened: https://example/pr/1" and bad == "Error: git push failed"
    assert seen[0] == ("acme/lumen", "user_p", "agent_p", "a.zip", "g.md")


def test_it_is_an_outward_write_that_waits_for_an_allow_and_is_offered_to_no_bot(monkeypatch):
    from tools import catalog, outbox
    from tools.writes import describe
    assert outbox.is_outward("publish_build") and describe("publish_build", {}).startswith("Open a pull request with the project's first build")
    assert "publish_build" not in [t["name"] for t in catalog.local_tools()]


def test_a_shared_chat_holds_it_for_the_askers_proceed(monkeypatch):
    from core import audience
    from tools import dispatch

    async def must_not_run(*a, **k):
        raise AssertionError("waits for the asker")
    monkeypatch.setattr(bpub, "publish", must_not_run)
    tokens = audience.set_audience(["other"], "chat")
    try:
        text = run(dispatch.run_tool("publish_build", {"archive": "a.zip"}, "", "acme/lumen", "agent_p"))
    finally:
        audience.reset_audience(tokens)
    assert text.startswith("NOT done yet")
