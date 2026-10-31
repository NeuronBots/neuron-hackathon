"""Fixing a PR review's findings: Muse changes the branch in a sandbox, nothing is pushed before a person allows it, and only to the PR's own branch."""
import asyncio
import json
import subprocess
import zipfile

import httpx
import pytest

from core.access import hash_password
from core.models import Agent, DocumentRecord, Message, Project, ProjectMember, Task, User
from core.usage import reset_usage_user, set_usage_user
from services import chat_rows, code_review as cr, first_build as fb, pr_fix

REAL_CLIENT = httpx.AsyncClient


def run(coro):
    return asyncio.run(coro)


def git(*args, cwd):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout


PATCH = """diff --git a/app.py b/app.py
--- a/app.py
+++ b/app.py
@@ -1 +1 @@
-print('bug')
+print('fixed')
"""


@pytest.fixture
def world(db, tmp_path, monkeypatch):
    monkeypatch.setattr("tools.documents.DOC_DIR", tmp_path / "docs")
    monkeypatch.setattr("tools.documents._r2", lambda: None)
    (tmp_path / "docs").mkdir()
    user = User(id="user_f", email="f@example.com", name="Fay", password_hash=hash_password("testpass123"), plan="free")
    proj = Project(id="proj_f", name="Lumen", repo="acme/lumen", coding_bot_id="agent_c")
    db.add_all([user, proj])
    db.commit()
    db.add(ProjectMember(project_id=proj.id, user_id=user.id, role="owner"))
    db.add(Agent(id="agent_c", name="Coder", system_prompt="x", project_id=proj.id))
    db.commit()

    bare = tmp_path / "origin.git"
    git("init", "-q", "--bare", "-b", "main", str(bare), cwd=tmp_path)
    seed = tmp_path / "seed"
    git("clone", "-q", str(bare), str(seed), cwd=tmp_path)
    (seed / "app.py").write_text("print('bug')\n")
    git("add", "-A", cwd=seed)
    git("-c", "user.name=Human", "-c", "user.email=h@example.com", "commit", "-q", "-m", "first", cwd=seed)
    git("push", "-q", "origin", "HEAD:main", "HEAD:feature", cwd=seed)

    real = cr.git_run

    async def git_run(*args, cwd, token="", timeout=300):
        return await real(*[str(bare) if a == "https://github.com/acme/lumen.git" else a for a in args], cwd=cwd, token=token, timeout=timeout)

    async def token(repo, uid):
        return "ghs_TOKEN"

    async def identity():
        return "neuron-bot", "neuron-bot[bot]@users.noreply.github.com"
    monkeypatch.setattr(cr, "git_run", git_run)
    monkeypatch.setattr(cr, "_token", token)
    monkeypatch.setattr("services.wiki._bot_identity", identity)

    state = {"requests": [], "pull": {}, "seed": seed}

    def head_sha():
        return git("rev-parse", "feature", cwd=bare).strip()

    def handler(request):
        state["requests"].append((request.method, request.url.path, json.loads(request.content) if request.content else None))
        if request.method == "GET":
            pr = {"number": 7, "state": "open", "title": "Add shifts", "head": {"ref": "feature", "sha": head_sha(), "repo": {"full_name": "acme/lumen"}}, **state["pull"]}
            return httpx.Response(200, json=pr)
        return httpx.Response(201, json={})
    monkeypatch.setattr(pr_fix.httpx, "AsyncClient", lambda **kw: REAL_CLIENT(transport=httpx.MockTransport(handler), **kw))
    state["bare"], state["head_sha"] = bare, head_sha
    ctx = set_usage_user(user.id, proj.id)
    yield user, proj, state
    reset_usage_user(ctx)


def save_patch(db, tmp_path, patch=PATCH, names=("fix.patch",), name="pr-7-fixes-20261029-100000.zip", project="proj_f"):
    with zipfile.ZipFile(tmp_path / "docs" / name, "w") as z:
        for n in names:
            z.writestr(n, patch)
    db.add(DocumentRecord(filename=name, agent_id="agent_c", project_id=project, user_id="user_f", created_by="user_f", shared_with=[], title="PR #7 fixes (patch)"))
    db.commit()
    return name


# ── the push, after a person said yes ────────────────────────────────

def test_the_fixes_are_one_commit_on_the_prs_own_branch_and_main_is_untouched(db, world, tmp_path):
    user, proj, state = world
    name = save_patch(db, tmp_path)
    out = run(pr_fix.push("acme/lumen", user.id, "agent_c", name, 7))
    bare = state["bare"]
    assert out.startswith("Pushed the fixes to feature of acme/lumen: 1 files in one commit")
    assert git("show", "feature:app.py", cwd=bare) == "print('fixed')\n" and git("show", "main:app.py", cwd=bare) == "print('bug')\n"
    assert git("rev-list", "--count", "feature", cwd=bare).strip() == "2"
    assert git("log", "feature", "-1", "--format=%an|%s", cwd=bare).strip() == "neuron-bot|Fix the review findings on #7"
    assert [r for r in state["requests"] if r[0] == "POST"][0][1] == "/repos/acme/lumen/issues/7/comments"  # and the PR is told


def test_a_pull_request_that_moved_on_a_fork_or_is_closed_is_not_pushed_to(db, world, tmp_path):
    user, proj, state = world
    name = save_patch(db, tmp_path)
    seed = state["seed"]
    (seed / "other.py").write_text("x\n")
    git("add", "-A", cwd=seed)
    git("-c", "user.name=H", "-c", "user.email=h@example.com", "commit", "-q", "-m", "more", cwd=seed)
    git("push", "-q", "origin", "HEAD:feature", cwd=seed)
    stale = {"sha": "0" * 40}
    state["pull"] = {"head": {"ref": "feature", "sha": stale["sha"], "repo": {"full_name": "acme/lumen"}}}  # GitHub says the head is older than the branch
    with pytest.raises(RuntimeError, match="new commits since"):
        run(pr_fix.push("acme/lumen", user.id, "agent_c", name, 7))
    state["pull"] = {"head": {"ref": "feature", "sha": state["head_sha"](), "repo": {"full_name": "someone/lumen"}}}
    with pytest.raises(fb.BuildError, match="fork"):
        run(pr_fix.push("acme/lumen", user.id, "agent_c", name, 7))
    state["pull"] = {"state": "closed"}
    with pytest.raises(fb.BuildError, match="closed"):
        run(pr_fix.push("acme/lumen", user.id, "agent_c", name, 7))
    assert git("show", "feature:app.py", cwd=state["bare"]) == "print('bug')\n"


def test_a_patch_that_does_not_apply_or_reaches_git_or_is_not_a_patch_changes_nothing(db, world, tmp_path):
    user, proj, state = world
    cases = [
        (PATCH.replace("-print('bug')", "-print('something else')"), ("fix.patch",), "no longer applies"),
        (PATCH.replace("app.py", ".git/config"), ("fix.patch",), "git's own files"),
        (PATCH.replace("b/app.py", "b/../up.py"), ("fix.patch",), "leaves the repository"),
        (PATCH, ("fix.patch", "extra.txt"), "not a fix patch"),
        (PATCH, ("other.patch",), "not a fix patch"),
    ]
    for i, (patch, names, message) in enumerate(cases):
        name = save_patch(db, tmp_path, patch, names, name=f"pr-7-fixes-2026102900000{i}.zip")
        with pytest.raises((RuntimeError, ValueError), match=message):
            run(pr_fix.push("acme/lumen", user.id, "agent_c", name, 7))
    assert git("show", "feature:app.py", cwd=state["bare"]) == "print('bug')\n"
    db.add(Project(id="proj_x", name="Elsewhere"))
    db.commit()
    with pytest.raises(ValueError, match="could not be found"):  # an archive of another project
        run(pr_fix.push("acme/lumen", user.id, "agent_c", save_patch(db, tmp_path, name="pr-7-fixes-20261029-110000.zip", project="proj_x"), 7))


# ── asking for the fixes ─────────────────────────────────────────────

def add_findings(db, tmp_path, name="pr-7-fix-prompt-20261029-090000.md"):
    (tmp_path / "docs" / name).write_text("Fix the review findings on acme/lumen#7\n1. [MAJOR] SQL injection")
    db.add(DocumentRecord(filename=name, agent_id="agent_c", project_id="proj_f", user_id="user_f", created_by="user_f", shared_with=[], title="PR #7 fix prompt: Add shifts"))
    db.commit()


def test_asking_to_fix_checks_the_project_the_findings_and_the_sandbox(db, world, tmp_path, monkeypatch):
    user, proj, state = world
    monkeypatch.setattr(pr_fix, "_need_sandbox", lambda: None)
    started = []

    async def fake(project_id, user_id, repo, info, pill):
        started.append((project_id, info["agent_id"], info["findings"][:20], info["pr"]["head"]["ref"]))
    monkeypatch.setattr(pr_fix, "_fix_and_tell", fake)

    with pytest.raises(fb.BuildError, match="no review findings"):
        run(pr_fix.start(proj.id, user.id, "acme/lumen", 7))
    add_findings(db, tmp_path)
    with pytest.raises(fb.BuildError, match="does not belong"):
        run(pr_fix.start(proj.id, user.id, "acme/other", 7))

    async def go():
        await pr_fix.start(proj.id, user.id, "acme/lumen", 7)
        await asyncio.gather(*pr_fix._running.values())
    run(go())
    assert started == [("proj_f", "agent_c", "Fix the review findi", "feature")]
    (row,) = db.query(Message).filter(Message.agent_id == "agent_c").all()
    assert '"kind": "prfix"' in row.content and '"status": "running"' in row.content


class FakeBox:
    patch = PATCH
    made = []

    def __init__(self, effort, timeout):
        FakeBox.made.append(self)
        self.fs, self.prompts = {}, []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return None

    async def load(self, tarball, files):
        self.fs.update(files)

    async def read(self, path):
        return self.fs.get(path)

    async def write(self, path, text):
        self.fs[path] = text

    async def baseline(self):
        self.based = True

    async def build(self, prompt):
        self.prompts.append(prompt)
        return "Fixed finding 1 and ran pytest: 4 passed."

    async def patch_(self):
        return FakeBox.patch


def test_a_finished_fix_is_saved_offered_for_a_push_and_told(db, world, tmp_path, monkeypatch):
    user, proj, state = world
    FakeBox.made = []
    FakeBox.patch_ = FakeBox.patch_
    monkeypatch.setattr(pr_fix, "BuildBox", type("B", (FakeBox,), {"patch": lambda self: FakeBox.patch_(self)}))
    monkeypatch.setattr(pr_fix.cr, "fetch_tarball", lambda *a: asyncio.sleep(0, result=b"tar"))
    told = []
    monkeypatch.setattr("routes.chat._start_kickoff", lambda *a, **k: told.append((a, k)))
    pr = {"number": 7, "title": "Add shifts", "head": {"ref": "feature", "sha": "abc"}}
    info = {"agent_id": "agent_c", "findings": "1. [MAJOR] SQL injection", "pr": pr, "project": "Lumen"}
    pill = chat_rows.write_row("agent_c", user.id, pr_fix._card("acme/lumen", pr, "running", stage="Starting"))

    run(pr_fix._fix_and_tell(proj.id, user.id, "acme/lumen", info, pill))

    box = FakeBox.made[0]
    assert box.fs[".neuron/FINDINGS.md"] == "1. [MAJOR] SQL injection" and "pull request #7" in box.prompts[0] and "SQL injection" not in box.prompts[0]  # the findings are data in a file
    card = json.loads(db.get(Message, pill).content[len("[[SOURCES:"):-2])
    assert card["status"] == "done" and card["files"] == 1 and card["waiting"] is True
    task = db.query(Task).filter(Task.agent_id == "agent_c").one()
    assert task.status == "decision" and task.title == "Approve: fixes for PR #7"
    stored = json.loads(next(e.text for e in task.events if e.text.startswith("[mail-draft] "))[len("[mail-draft] "):])
    assert stored["tool"] == "push_pr_fix" and stored["args"]["number"] == 7 and stored["args"]["archive"].startswith("pr-7-fixes-") and stored["repo"] == "acme/lumen"
    assert (tmp_path / "docs").glob("pr-7-fixes-*.zip") and told[0][1]["no_tools"] is True and "A card in this chat" in told[0][0][3]


def test_when_muse_changes_nothing_or_fails_the_person_hears_it_and_no_push_is_offered(db, world, tmp_path, monkeypatch):
    user, proj, state = world
    monkeypatch.setattr(pr_fix, "BuildBox", type("B", (FakeBox,), {"patch": lambda self: asyncio.sleep(0, result="")}))
    monkeypatch.setattr(pr_fix.cr, "fetch_tarball", lambda *a: asyncio.sleep(0, result=b"tar"))
    told = []
    monkeypatch.setattr("routes.chat._start_kickoff", lambda *a, **k: told.append(a[3]))
    pr = {"number": 7, "title": "t", "head": {"ref": "feature", "sha": "abc"}}
    info = {"agent_id": "agent_c", "findings": "f", "pr": pr, "project": "Lumen"}
    pill = chat_rows.write_row("agent_c", user.id, pr_fix._card("acme/lumen", pr, "running"))
    run(pr_fix._fix_and_tell(proj.id, user.id, "acme/lumen", info, pill))
    assert "changed nothing" in told[0] and json.loads(db.get(Message, pill).content[len("[[SOURCES:"):-2])["files"] == 0 and not db.query(Task).count()

    async def boom(*a, **k):
        raise RuntimeError("the build took too long and was stopped")
    monkeypatch.setattr(pr_fix, "fix", boom)
    run(pr_fix._fix_and_tell(proj.id, user.id, "acme/lumen", info, pill))
    assert "did not finish: the build took too long" in told[1] and json.loads(db.get(Message, pill).content[len("[[SOURCES:"):-2])["status"] == "failed"


def test_the_allow_card_pushes_through_the_dispatcher_and_a_shared_chat_holds_it(db, world, tmp_path, monkeypatch):
    from core import audience
    from tools import dispatch
    user, proj, state = world
    name = save_patch(db, tmp_path)
    out = run(dispatch.run_tool("push_pr_fix", {"archive": name, "number": 7}, "", "acme/lumen", "agent_c"))
    assert out.startswith("Pushed the fixes to feature")
    assert run(dispatch.run_tool("push_pr_fix", {"archive": "nope.zip", "number": 7}, "", "acme/lumen", "agent_c")).startswith("Error: ")
    tokens = audience.set_audience(["other"], "chat")
    try:
        assert run(dispatch.run_tool("push_pr_fix", {"archive": name, "number": 7}, "", "acme/lumen", "agent_c")).startswith("NOT done yet")
    finally:
        audience.reset_audience(tokens)


def test_the_button_endpoint_finds_the_members_project_and_maps_refusals(db, world, monkeypatch):
    from fastapi import HTTPException
    from routes import projects
    user, proj, state = world
    seen = []

    async def fake(project_id, user_id, repo, number):
        seen.append((project_id, repo, number))
        if number == 9:
            raise fb.BuildError("Fixes for that pull request are already being made.", 409)
    monkeypatch.setattr(projects.pr_fix, "start", fake)
    assert run(projects.start_pr_fix(projects.FixPrBody(repo=" Acme/Lumen ", number=7), user=user, db=db)) == {"started": True} and seen == [("proj_f", "Acme/Lumen", 7)]
    with pytest.raises(HTTPException) as err:
        run(projects.start_pr_fix(projects.FixPrBody(repo="acme/lumen", number=9), user=user, db=db))
    assert err.value.status_code == 409
    stranger = User(id="user_s", email="s@example.com", name="S", password_hash="x", plan="free")
    db.add(stranger)
    db.commit()
    with pytest.raises(HTTPException) as err:
        run(projects.start_pr_fix(projects.FixPrBody(repo="acme/lumen", number=7), user=stranger, db=db))
    assert err.value.status_code == 404


def test_after_a_push_the_live_session_measures_its_next_change_from_the_pushed_branch(db, world, tmp_path):
    from services import preview as pv
    user, proj, state = world
    name = save_patch(db, tmp_path)
    calls = []

    class Box:
        async def baseline(self):
            calls.append("baseline")
    live = pv.Preview("proj_f", "user_f", "x.zip")
    live.session, live.status, live.box = True, "running", Box()
    pv._previews[live.id] = live
    try:
        run(pr_fix.push("acme/lumen", user.id, "agent_c", name, 7))
    finally:
        pv._previews.clear()
    assert calls == ["baseline"]  # and a push with no session running simply works, as the other tests show
