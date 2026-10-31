"""The wiki build: what Muse returns is cleaned, the pages are composed against what the wiki already holds, and the result is pushed to a real git remote."""
import asyncio
import contextlib
import json
import subprocess

import pytest

from core.access import hash_password
from core.models import Agent, Message, Project, ProjectMember, Task, User
from core.usage import reset_usage_user, set_usage_user
from services import code_review as cr, wiki
from tools import outbox

SHA = "a" * 40
MARK = wiki.MARKER.encode()


def run(coro):
    return asyncio.run(coro)


def page(file, section="Modules", title=None, summary="Why you would read it.", sources=("src/a.py",)):
    return {"file": file, "title": title or file[:-3].replace("-", " "), "section": section, "summary": summary, "covers": "everything", "sources": list(sources)}


def plan_of(*pages, start_here=()):
    return {"overview": "A thing | with a pipe.", "stack": "Python", "languages": "Python", "entry_points": "main.py", "data_stores": "Postgres", "deployment": "Render",
            "start_here": list(start_here), "pages": list(pages)}


BODY = "> **Summary:** a page.\n\n## Heading\nSome text that is long enough to count as a page of the wiki.\n"


# ── cleaning ──────────────────────────────────────────────────────────

def test_a_plan_keeps_only_well_named_unique_pages_in_known_sections():
    raw = json.dumps(plan_of(
        page("Modules-Billing.md"), page("Modules-Billing.md"), page("modules-lower.md"), page("Home.md"), page("_Sidebar.md"), page("Sub/Dir.md"),
        page("Notes.md", section="Made-Up"), page("Glossary.md", section="Glossary"), "junk", page("Reference-Env.md", section="Reference")))
    got = wiki.clean_plan(raw)
    assert [p["file"] for p in got["pages"]] == ["Modules-Billing.md", "Glossary.md", "Reference-Env.md"]
    with pytest.raises(ValueError):
        wiki.clean_plan(json.dumps(plan_of(page("bad name.md"))))
    with pytest.raises(ValueError):
        wiki.clean_plan("[1]")


def test_a_plan_is_capped_and_unknown_facts_say_so(monkeypatch):
    monkeypatch.setattr(wiki, "MAX_PAGES", 2)
    data = plan_of(*(page(f"Modules-M{i}.md") for i in range(5)))
    data["stack"] = ""
    got = wiki.clean_plan(json.dumps(data))
    assert len(got["pages"]) == 2 and got["stack"] == "Not determined from the code"


def test_a_page_must_have_real_content():
    assert wiki.clean_page(json.dumps({"content": BODY}))["content"] == BODY
    for bad in ('{"content": "short"}', '{"content": 3}', "[]"):
        with pytest.raises(ValueError):
            wiki.clean_page(bad)


def test_credentials_are_redacted_whatever_the_prompt_said():
    text = "API_KEY=abcdefghijklmnopqrstuvwx and token: 'zzzzzzzzzzzzzzzzzzzzzzzz' and sk-abcdefghijklmnopqrstuvwxyz and ghp_" + "a" * 36
    got = wiki.redact(text)
    assert "abcdefghijklmnopqrstuvwx" not in got and "zzzzzzzz" not in got and "sk-abc" not in got and "ghp_a" not in got and got.count("<redacted>") == 4
    assert wiki.redact("DATABASE_URL is required") == "DATABASE_URL is required"  # names are fine; only values go


def test_links_to_missing_pages_become_text_and_table_links_change_shape():
    text = "See [[Billing|Modules-Billing]] and [[Gone|Modules-Gone]].\n| a | [[Billing|Modules-Billing]] |\n`[[code|Modules-Gone]]`\n```\n[[Fenced|Modules-Gone]]\n```"
    got = wiki.tidy_links(text, {"Modules-Billing"})
    assert "[[Billing|Modules-Billing]] and Gone." in got
    assert "| [Billing](Modules-Billing) |" in got
    assert "`[[code|Modules-Gone]]`" in got and "[[Fenced|Modules-Gone]]" in got  # code is left alone


def test_a_finished_page_has_no_h1_clean_links_and_the_mark():
    got = wiki.finish("# Title\n\n" + BODY + "[[Nope|Missing]]\n", {"Modules-A"})
    assert not got.startswith("#") and "[[Nope" not in got and got.rstrip().endswith(wiki.MARKER)


# ── composing against what is already there ──────────────────────────

def test_people_pages_are_never_overwritten_and_stay_reachable():
    human = b"Our team's own page."
    existing = {"Our-Process.md": human, "Modules-Billing.md": b"Hand-written billing notes.", "Old-Bot-Page.md": b"old\n" + MARK,
                "Home.md": b"Our team's real home page. " * 40, "Release-Notes/2026-01-01-PR-1.md": b"notes " + MARK, "image.png": b"\x89PNG"}
    plan = plan_of(page("Modules-Billing.md"), page("Modules-Auth.md"))
    out, info = wiki.compose(existing, "proj", plan, {"Modules-Billing.md": BODY, "Modules-Auth.md": BODY}, SHA)

    assert "Modules-Billing.md" not in out and info["skipped"] == ["Modules-Billing.md"]  # a person's page of that name wins
    assert "Modules-Auth.md" in out and out["Modules-Auth.md"].rstrip().endswith(wiki.MARKER)
    assert "Home.md" not in out  # a hand-written Home is theirs
    assert not any(f.startswith("Release-Notes/") or f in ("Our-Process.md", "image.png") for f in out)
    assert set(info["protected"]) == {"Our-Process.md", "Modules-Billing.md", "Home.md", "image.png"}
    sidebar = out["_Sidebar.md"]
    assert "**More**" in sidebar and "[[Our Process|Our-Process]]" in sidebar and "[[Old Bot Page|Old-Bot-Page]]" in sidebar and info["stale"] == ["Old-Bot-Page.md"]
    assert sidebar.index("[[Home]]") < sidebar.index("[[Modules Auth|Modules-Auth]]")


def test_a_fresh_wikis_placeholder_chrome_is_taken_over_and_releases_in_the_sidebar_are_kept():
    old_sidebar = ("Placeholder\n" + wiki._SB_START + "\n**Releases**\n- [x](Release-Notes/x)\n" + wiki._SB_END).encode()
    existing = {"Home.md": b"Welcome to the wiki!", "_Sidebar.md": old_sidebar.replace(b"Placeholder", b"P")}
    out, info = wiki.compose(existing, "proj", plan_of(page("Modules-A.md")), {"Modules-A.md": BODY}, SHA)
    assert {"Home.md", "_Sidebar.md", "_Footer.md"} <= set(out) and "Home.md" not in info["protected"]
    assert wiki._SB_START in out["_Sidebar.md"] and "Release-Notes/x" in out["_Sidebar.md"]
    assert SHA[:8] in out["_Footer.md"]


def test_the_chrome_always_matches_the_pages_that_exist():
    pages = [page("Getting-Started-Setup.md", "Getting Started"), page("Architecture-Overview.md", "Architecture"), page("Glossary.md", "Glossary"),
             page("Modules-Billing.md", summary="Money | and more.")]
    out, _ = wiki.compose({}, "proj", plan_of(*pages, start_here=("Architecture-Overview", "Nope.md")), {p["file"]: BODY for p in pages}, SHA)
    home, side = out["Home.md"], out["_Sidebar.md"]
    assert home.startswith("A thing | with a pipe.") and "| Stack | Python |" in home  # prose keeps its pipe...
    assert "Money / and more." in home and "Money | and more." not in home  # ...but one in a table cell would split the cell
    assert "- [[Architecture Overview|Architecture-Overview]]" in home.split("## Start here")[1].split("## Wiki map")[0]
    assert "[Modules Billing](Modules-Billing)" in home and "[Glossary](Glossary)" in home
    order = [side.index(x) for x in ("[[Home]]", "**Getting Started**", "**Architecture**", "**Modules**", "**Glossary**")]
    assert order == sorted(order)
    assert all(f"({p['file'][:-3]})" in home for p in pages)


# ── a real build against a real git remote ───────────────────────────

class FakeBox:
    def __init__(self, plan, fail_pages=()):
        self.plan, self.fail_pages, self.calls = plan, set(fail_pages), []

    async def muse(self, text, schema):
        self.calls.append(text)
        if "pages" in schema["properties"]:
            return json.dumps(self.plan)
        title = next(line.split(": ", 1)[1] for line in text.splitlines() if line.startswith("- File: "))
        if title.strip("`") in self.fail_pages:
            raise RuntimeError("Muse failed to run")
        return json.dumps({"content": f"> **Summary:** {title}\n\n## Heading\nA body for the page that is long enough to be kept.\n"})


def git(*args, cwd):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout


@pytest.fixture
def remote(tmp_path, monkeypatch):
    """A bare 'wiki' repo holding a person's page and a placeholder Home, standing in for github.com/<repo>.wiki.git."""
    bare = tmp_path / "wiki.git"
    git("init", "-q", "--bare", "-b", "master", str(bare), cwd=tmp_path)
    seed = tmp_path / "seed"
    git("clone", "-q", str(bare), str(seed), cwd=tmp_path)
    (seed / "Our-Process.md").write_text("How we work, in our words.")
    (seed / "Home.md").write_text("Welcome to the wiki!")
    git("add", "-A", cwd=seed)
    git("-c", "user.name=Human", "-c", "user.email=h@example.com", "commit", "-q", "-m", "first page", cwd=seed)
    git("push", "-q", "origin", "HEAD:master", cwd=seed)
    monkeypatch.setattr(wiki, "_wiki_url", lambda repo: str(bare))
    monkeypatch.setattr(wiki.github_app, "configured", lambda: False)  # never ask GitHub who the App is

    async def token(repo, user):
        return "ghs_TOKEN"

    async def head(repo, tok):
        return SHA
    monkeypatch.setattr(cr, "_token", token)
    monkeypatch.setattr(wiki, "head_sha", head)

    def serve(box):
        @contextlib.asynccontextmanager
        async def open_box(repo, sha, tok, files=None, effort=""):
            assert files is None  # a wiki build has no PR diff
            yield box
        monkeypatch.setattr(cr, "open_box", open_box)
    serve.bare, serve.tmp = bare, tmp_path
    return serve


def checkout_of(remote):
    dest = remote.tmp / "view"
    if dest.exists():
        import shutil
        shutil.rmtree(dest)
    git("clone", "-q", str(remote.bare), str(dest), cwd=remote.tmp)
    return dest


PLAN = plan_of(page("Getting-Started-Setup.md", "Getting Started"), page("Modules-Billing.md"), page("Modules-Auth.md"), page("Glossary.md", "Glossary"),
               start_here=("Getting-Started-Setup",))


def test_a_build_writes_the_wiki_and_leaves_a_persons_pages_alone(remote):
    remote(FakeBox(PLAN))
    result = run(wiki.build("acme/proj", "u1"))

    view = checkout_of(remote)
    assert (view / "Our-Process.md").read_text() == "How we work, in our words."  # byte for byte
    for f in ("Modules-Billing.md", "Modules-Auth.md", "Getting-Started-Setup.md", "Glossary.md", "Home.md", "_Sidebar.md", "_Footer.md"):
        assert wiki.MARKER in (view / f).read_text(), f
    assert "Welcome to the wiki!" not in (view / "Home.md").read_text()  # the placeholder was taken over
    assert "[[Our Process|Our-Process]]" in (view / "_Sidebar.md").read_text()
    assert (result["pages"], result["changed"], result["failed"], result["url"]) == (4, True, [], "https://github.com/acme/proj/wiki")
    assert git("log", "-1", "--format=%s", cwd=view).strip() == "Build wiki from acme/proj@aaaaaaaa"
    assert git("log", "-1", "--format=%an <%ae>", cwd=view).strip() == "neuron-bot <neuron-bot[bot]@users.noreply.github.com>"


def test_building_again_with_nothing_new_changes_nothing(remote):
    remote(FakeBox(PLAN))
    run(wiki.build("acme/proj", "u1"))
    before = git("rev-parse", "HEAD", cwd=checkout_of(remote)).strip()
    result = run(wiki.build("acme/proj", "u1"))
    assert result["changed"] is False and git("rev-parse", "HEAD", cwd=checkout_of(remote)).strip() == before


def test_an_update_rewrites_the_bots_pages_but_not_one_a_person_took_over(remote):
    remote(FakeBox(PLAN))
    run(wiki.build("acme/proj", "u1"))
    view = checkout_of(remote)
    (view / "Modules-Billing.md").write_text("Billing, rewritten by a person.")  # the mark is gone: it is theirs now
    git("add", "-A", cwd=view)
    git("-c", "user.name=Human", "-c", "user.email=h@example.com", "commit", "-q", "-m", "edit", cwd=view)
    git("push", "-q", "origin", "HEAD:master", cwd=view)

    result = run(wiki.build("acme/proj", "u1"))

    assert (checkout_of(remote) / "Modules-Billing.md").read_text() == "Billing, rewritten by a person."
    assert result["protected"].count("Modules-Billing.md") == 1 and result["skipped"] == ["Modules-Billing.md"]


def test_an_edit_made_during_the_build_is_kept_and_the_push_is_retried_never_forced(remote, monkeypatch):
    remote(FakeBox(PLAN))
    real, raced = wiki._git, []

    async def racing(*args, cwd, **kw):
        if args[0] == "push" and not raced:
            raced.append(1)  # someone saves a page on GitHub just before our first push
            other = remote.tmp / "other"
            git("clone", "-q", str(remote.bare), str(other), cwd=remote.tmp)
            (other / "Late-Page.md").write_text("Added while the build ran.")
            git("add", "-A", cwd=other)
            git("-c", "user.name=Human", "-c", "user.email=h@example.com", "commit", "-q", "-m", "late", cwd=other)
            git("push", "-q", "origin", "HEAD:master", cwd=other)
        return await real(*args, cwd=cwd, **kw)
    monkeypatch.setattr(wiki, "_git", racing)

    result = run(wiki.build("acme/proj", "u1"))

    view = checkout_of(remote)
    assert raced and (view / "Late-Page.md").read_text() == "Added while the build ran." and wiki.MARKER in (view / "Modules-Auth.md").read_text()
    assert "[[Late Page|Late-Page]]" in (view / "_Sidebar.md").read_text() and result["changed"]


def test_pages_that_fail_are_left_out_and_too_many_failures_publish_nothing(remote):
    remote(FakeBox(PLAN, fail_pages={"Modules-Auth.md"}))
    result = run(wiki.build("acme/proj", "u1"))
    view = checkout_of(remote)
    assert result["failed"] == ["Modules-Auth.md"] and not (view / "Modules-Auth.md").exists()
    assert "Modules-Auth" not in (view / "_Sidebar.md").read_text() and "Modules-Auth" not in (view / "Home.md").read_text()

    before = git("rev-parse", "HEAD", cwd=view).strip()
    remote(FakeBox(PLAN, fail_pages={"Getting-Started-Setup.md", "Modules-Billing.md", "Modules-Auth.md"}))
    with pytest.raises(RuntimeError, match="nothing was published"):
        run(wiki.build("acme/proj", "u1"))
    assert git("rev-parse", "HEAD", cwd=checkout_of(remote)).strip() == before


def test_an_unreachable_wiki_says_how_to_fix_it(remote, monkeypatch, tmp_path):
    monkeypatch.setattr(wiki, "_wiki_url", lambda repo: str(tmp_path / "nothing-here.git"))
    with pytest.raises(wiki.WikiError, match="first page"):
        run(wiki.reachable("acme/proj", "t"))


# ── the tool, and the permission it needs ────────────────────────────

@pytest.fixture
def world(db):
    user = User(id="user_wiki", email="w@example.com", name="Wiki", password_hash=hash_password("testpass123"), plan="free")
    proj = Project(id="proj_wiki", name="Wiki", repo="acme/proj")
    db.add_all([user, proj])
    db.commit()
    db.add(ProjectMember(project_id=proj.id, user_id=user.id, role="owner"))
    bot = Agent(id="agent_wiki", name="Scribe", system_prompt="x", project_id=proj.id)
    db.add(bot)
    db.commit()
    token = set_usage_user(user.id, proj.id)
    yield user, proj, bot
    reset_usage_user(token)


def test_start_needs_a_linked_repo_and_a_reachable_wiki(world, monkeypatch):
    user, proj, bot = world
    from tools.scoping import NO_REPO_LINK
    assert run(wiki.start(bot.id, "")) == NO_REPO_LINK

    async def token(repo, uid):
        return "t"

    async def unreachable(repo, tok):
        raise wiki.WikiError("Could not reach the wiki for acme/proj.")
    monkeypatch.setattr(cr, "_token", token)
    monkeypatch.setattr(wiki, "reachable", unreachable)
    out = run(wiki.start(bot.id, "acme/proj"))
    assert out.startswith("Error: Could not reach the wiki") and "do not retry" in out and not wiki._running


def _cards(db, bot):
    """The bot's chat as the person sees it: (kind, parsed card) for pills, (None, text) for plain rows."""
    out = []
    for m in db.query(Message).filter(Message.agent_id == bot.id).order_by(Message.id).all():
        out.append(("card", json.loads(m.content[len("[[SOURCES:"):-2])) if m.content.startswith("[[SOURCES:") else ("text", m.content))
    return out


def test_start_builds_in_the_background_then_the_person_and_the_bot_are_told(db, world, monkeypatch):
    user, proj, bot = world
    told, calls, seen = [], [], []

    async def token(repo, uid):
        return "t"

    async def ok(repo, tok):
        return None

    async def fake_build(repo, uid, focus="", progress=None):
        calls.append((repo, uid, focus))
        seen.append(_cards(db, bot))  # what the person sees while it runs
        await progress("Planning the pages")
        await progress("Planning the pages")  # the same stage twice writes once
        await progress("Writing pages: 3 of 4 done")
        seen.append(_cards(db, bot))
        return {"pages": 4, "protected": ["Our-Process.md"], "skipped": [], "stale": [], "failed": ["Modules-X.md"], "changed": True, "sha": SHA,
                "url": "https://github.com/acme/proj/wiki", "planned": 5}
    monkeypatch.setattr(cr, "_token", token)
    monkeypatch.setattr(wiki, "reachable", ok)
    monkeypatch.setattr(wiki, "build", fake_build)
    monkeypatch.setattr("routes.chat._start_kickoff", lambda *a, **k: told.append((a, k)))

    async def go():
        first = await wiki.start(bot.id, "acme/proj", "the API  \n only")
        again = await wiki.start(bot.id, "ACME/proj")
        await asyncio.gather(*wiki._running.values())
        return first, again

    first, again = run(go())

    assert first.startswith("Started building the wiki") and "Do not call build_wiki again" in first
    assert again.startswith("A wiki build for ACME/proj is already running")
    assert calls == [("acme/proj", user.id, "the API only")]  # blanks in the focus are squeezed
    # while it ran: one pill, spinning, and it moves on in place instead of piling up
    (kind, running), = seen[0]
    assert (kind, running["kind"], running["status"], running["stage"], running["repo"]) == ("card", "wiki", "running", "Starting", "acme/proj")
    (kind, moved), = seen[1]
    assert moved["status"] == "running" and moved["stage"] == "Writing pages: 3 of 4 done"
    # when it ended: the same pill says how it went, and a plain row with the link follows, written without any model
    (_, pill), (kind, file_card) = _cards(db, bot)
    assert (pill["status"], pill["pages"], pill["untouched"], pill["failed"], pill["url"], pill["changed"]) == ("done", 4, 1, 1, "https://github.com/acme/proj/wiki", True)
    assert (kind, file_card) == ("card", {"kind": "wikidoc", "repo": "acme/proj", "url": "https://github.com/acme/proj/wiki", "pages": 4, "untouched": 1, "changed": True})
    (args, kwargs), = told
    assert args[0] == bot.id and args[2] == proj.id and "4 pages written" in args[3] and "https://github.com/acme/proj/wiki" in args[3] and "1 existing pages" in args[3]
    assert kwargs == {"no_tools": True} and not wiki._running


def test_a_failed_build_updates_the_pill_and_is_reported_in_plain_words(db, world, monkeypatch):
    user, proj, bot = world
    told = []

    async def token(repo, uid):
        return "t"

    async def ok(repo, tok):
        return None

    async def broken(repo, uid, focus="", progress=None):
        raise wiki.WikiError("GitHub refused the push to the wiki.")
    monkeypatch.setattr(cr, "_token", token)
    monkeypatch.setattr(wiki, "reachable", ok)
    monkeypatch.setattr(wiki, "build", broken)
    monkeypatch.setattr("routes.chat._start_kickoff", lambda *a, **k: told.append(a))

    async def go():
        await wiki.start(bot.id, "acme/proj")
        await asyncio.gather(*wiki._running.values())
    run(go())

    (_, pill), = _cards(db, bot)  # a failure has nothing to open, so no file card
    assert pill["status"] == "failed" and pill["reason"] == "GitHub refused the push to the wiki."
    assert "did not work: GitHub refused the push to the wiki." in told[0][3]


def test_a_build_stopped_by_a_restart_does_not_leave_the_pill_spinning(db, world, monkeypatch):
    user, proj, bot = world

    async def token(repo, uid):
        return "t"

    async def ok(repo, tok):
        return None

    async def forever(repo, uid, focus="", progress=None):
        await asyncio.sleep(60)
    monkeypatch.setattr(cr, "_token", token)
    monkeypatch.setattr(wiki, "reachable", ok)
    monkeypatch.setattr(wiki, "build", forever)

    async def go():
        await wiki.start(bot.id, "acme/proj")
        task = wiki._running["acme/proj"]
        await asyncio.sleep(0.05)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    run(go())
    (_, pill), = _cards(db, bot)
    assert pill["status"] == "failed" and "restarting" in pill["reason"]


def test_nobody_to_report_to_means_no_build(db, world, monkeypatch):
    user, proj, bot = world

    async def token(repo, uid):
        return "t"

    async def ok(repo, tok):
        return None
    monkeypatch.setattr(cr, "_token", token)
    monkeypatch.setattr(wiki, "reachable", ok)
    from core import usage
    outer = usage._user_id.set("")
    try:
        out = run(wiki.start(bot.id, "acme/proj"))
    finally:
        usage._user_id.reset(outer)
    assert out.startswith("Error:") and "signed-in person" in out and not wiki._running and _cards(db, bot) == []


@pytest.mark.parametrize("runner,first", [("local", "Reading the repository"), ("modal", "Starting the sandbox and reading the repository")])
def test_a_build_reports_each_stage_in_order(remote, monkeypatch, runner, first):
    monkeypatch.setenv("REVIEW_RUNNER", runner)  # pinned: a developer's own .env must not change what is expected
    remote(FakeBox(PLAN))
    stages = []

    async def progress(stage):
        stages.append(stage)
    run(wiki.build("acme/proj", "u1", "", progress))
    writing = [x for x in stages if x.startswith("Writing pages")]
    assert stages[0] == first and stages[1] == "Planning the pages" and stages[-1] == "Publishing to the wiki"
    assert writing[0] == "Writing pages: 0 of 4 done" and writing[-1] == "Writing pages: 4 of 4 done" and len(writing) == 5


def test_the_wiki_tool_is_offered_and_goes_through_the_dispatcher(monkeypatch):
    from tools import catalog, dispatch
    assert "build_wiki" in [t["name"] for t in catalog.local_tools()]
    seen = []

    async def fake_start(agent_id, repo, focus=""):
        seen.append((agent_id, repo, focus))
        return "started"
    monkeypatch.setattr(wiki, "start", fake_start)
    assert run(dispatch.run_tool("build_wiki", {"focus": "api"}, "ABC", "acme/proj", "agent_1")) == "started"
    assert seen == [("agent_1", "acme/proj", "api")]


def test_a_shared_chat_holds_the_build_for_the_askers_proceed(monkeypatch):
    from core import audience
    from tools import dispatch

    async def must_not_run(*a, **k):
        raise AssertionError("the build must wait for the asker")
    monkeypatch.setattr(wiki, "start", must_not_run)
    tokens = audience.set_audience(["other"], "chat")
    try:
        text = run(dispatch.run_tool("build_wiki", {}, "", "acme/proj", "agent_1"))
        held = audience.actions()
    finally:
        audience.reset_audience(tokens)
    assert text.startswith("NOT done yet") and [c["tool"] for c in held] == ["build_wiki"] and held[0]["repo"] == "acme/proj"


def test_a_bot_needs_a_persons_permission_and_is_asked_in_the_card(db, world):
    user, proj, bot = world
    from tools.writes import describe
    assert outbox.is_outward("build_wiki")
    assert describe("build_wiki", {"focus": "the API"}).startswith("Build or update this project's GitHub wiki, focusing on: the API. Pages people wrote are left untouched")

    text, event = outbox.queue_outward(bot.id, "build_wiki", {"focus": "the API"}, "", "acme/proj", db, interactive=True)

    assert "a card in this chat" in text and event["type"] == "task_decision"
    task = db.query(Task).filter(Task.agent_id == bot.id).one()
    assert task.status == "decision" and task.title.startswith("Approve: Build or update this project's GitHub wiki")
    stored = json.loads(next(e.text for e in task.events if e.text.startswith(outbox.MAIL_DRAFT))[len(outbox.MAIL_DRAFT):])
    assert stored == {"tool": "build_wiki", "args": {"focus": "the API"}, "jira_key": "", "repo": "acme/proj"}  # approving builds this project's wiki, no other
    assert db.query(Message).filter(Message.agent_id == bot.id).count() == 1


def test_commits_are_made_as_the_github_app_when_it_is_set_up(monkeypatch):
    async def login():
        return "neuron-code-bot"
    monkeypatch.setattr(wiki.github_app, "configured", lambda: True)
    monkeypatch.setattr(wiki.github_app, "bot_login", login)
    assert run(wiki._bot_identity()) == ("neuron-code-bot", "neuron-code-bot[bot]@users.noreply.github.com")

    async def down():
        raise RuntimeError("GitHub is down")
    monkeypatch.setattr(wiki.github_app, "bot_login", down)
    assert run(wiki._bot_identity())[0] == "neuron-bot"  # a failed lookup never stops a build
