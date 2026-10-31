"""The live preview: the app runs in a sandbox, the browser only ever sees our own address, and only with the preview's key."""
import asyncio
import json

import httpx
import pytest

from core.access import hash_password
from core.models import Agent, Project, ProjectMember, User
from services import first_build as fb, preview as pv


def run(coro):
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def clean():
    pv._previews.clear()
    yield
    pv._previews.clear()


def running(project="proj_a", url="https://ta-secret-8080.w.modal.host"):
    p = pv.Preview(project, "user_a", "lumen.zip")
    p.status, p.url = "running", url
    p.client = httpx.AsyncClient(transport=httpx.MockTransport(lambda req: pv_handler(req)))
    pv._previews[p.id] = p
    return p


seen = []


def pv_handler(request):
    seen.append(request)
    if request.url.path == "/":
        return httpx.Response(200, headers={"content-type": "text/html", "set-cookie": "sid=1", "x-frame-options": "DENY", "content-security-policy": "default-src 'none'"},
                              text='<html><head><title>x</title></head><body><a href="/about">a</a><img src="/img/a.png"><form action="/api/shifts"><a href="//cdn.example/x">c</a></form></body></html>')
    if request.url.path == "/go":
        return httpx.Response(302, headers={"location": "/done"})
    return httpx.Response(200, json={"ok": True})


def test_html_links_that_start_at_the_root_are_pointed_back_into_the_preview():
    out = pv.rewrite_html('<head><title>t</title></head><a href="/a">x</a><a href=\'/b\'>y</a><a href="//cdn/x">z</a><a href="https://e.com/">w</a><a href="rel">r</a>', "/api/preview/i/k/")
    assert 'href="/api/preview/i/k/a"' in out and "href='/api/preview/i/k/b'" in out
    assert 'href="//cdn/x"' in out and 'href="https://e.com/"' in out and 'href="rel"' in out  # other sites and relative links are left alone
    assert out.index("<script>") < out.index("<title>") and '"/api/preview/i/k/"' in out  # the script that fixes fetch() runs first


def test_the_app_is_reached_without_the_viewers_cookies_and_without_its_own_framing_rules():
    seen.clear()
    p = running()
    status, headers, body = run(pv.forward(p, "GET", "", "", {"cookie": "neuron=SESSION", "authorization": "Bearer x", "accept": "text/html", "host": "evil"}, b""))
    req = seen[0]
    assert status == 200 and "cookie" not in req.headers and "authorization" not in req.headers and str(req.url) == "https://ta-secret-8080.w.modal.host/"
    assert not {"set-cookie", "x-frame-options", "content-security-policy"} & {k.lower() for k in headers}
    assert b"/api/preview/" in body and b"ta-secret" not in body and "ta-secret" not in json.dumps(headers)  # the sandbox's address is nowhere in what the browser gets


def test_redirects_stay_inside_the_preview_and_queries_and_bodies_are_forwarded():
    seen.clear()
    p = running()
    _, headers, _ = run(pv.forward(p, "GET", "go", "", {}, b""))
    assert headers["location"] == f"/api/preview/{p.id}/{p.key}/done"
    run(pv.forward(p, "POST", "api/shifts", "a=1&b=2", {"content-type": "application/json"}, b'{"x": 1}'))
    assert seen[-1].method == "POST" and str(seen[-1].url).endswith("/api/shifts?a=1&b=2") and seen[-1].content == b'{"x": 1}'


def test_a_preview_that_is_not_running_or_whose_app_is_down_says_so():
    p = running()
    p.status = "stopped"
    assert run(pv.forward(p, "GET", "", "", {}, b""))[0] == 503

    def boom(req):
        raise httpx.ConnectError("down")
    q = running()
    q.client = httpx.AsyncClient(transport=httpx.MockTransport(boom))
    assert run(pv.forward(q, "GET", "", "", {}, b""))[0] == 502


def test_only_the_right_key_opens_a_preview_and_only_a_member_sees_its_status():
    p = running()
    assert pv.by_key(p.id, p.key) is p and pv.by_key(p.id, "x" * len(p.key)) is None and pv.by_key("nope", p.key) is None
    assert pv.get(p.id, {"proj_a"}) is p and pv.get(p.id, {"proj_b"}) is None
    assert p.view()["src"] == f"/api/preview/{p.id}/{p.key}/" and "url" not in p.view() and "modal.host" not in json.dumps(p.view())
    p.status = "starting"
    assert p.view()["src"] == ""  # no frame until the app answers


def test_how_to_start_is_read_from_musics_answer_and_nothing_else_is_trusted():
    assert pv.parse_recipe('ok {"install": " pip install -r requirements.txt ", "start": "python app.py", "notes": "n"} done') == {"install": "pip install -r requirements.txt", "start": "python app.py"}
    assert pv.parse_recipe('{"install": "", "start": "npm start", "notes": ""}')["install"] == ""
    assert pv.parse_recipe('{"install": "x", "start": "  ", "notes": ""}') is None and pv.parse_recipe("no json") is None and pv.parse_recipe('{"start": 5}') is None
    assert str(pv.PREVIEW_PORT) in pv.recipe_prompt() and "0.0.0.0" in pv.recipe_prompt()


@pytest.fixture
def world(db):
    user = User(id="user_a", email="a@example.com", name="A", password_hash=hash_password("testpass123"), plan="free")
    other = User(id="user_o", email="o@example.com", name="O", password_hash=hash_password("testpass123"), plan="free")
    db.add_all([user, other, Project(id="proj_a", name="Lumen", coding_bot_id="agent_c")])
    db.commit()
    db.add_all([ProjectMember(project_id="proj_a", user_id="user_a", role="owner"), Agent(id="agent_c", name="Coder", system_prompt="x", project_id="proj_a")])
    db.commit()
    return user, other


def test_the_endpoints_answer_members_only_and_the_proxy_needs_no_session_but_the_key(db, world, monkeypatch):
    from fastapi import HTTPException
    from routes import projects
    user, other = world
    p = running()
    assert projects.preview_status(p.id, user=user, db=db)["status"] == "running"
    for who in (other,):
        with pytest.raises(HTTPException) as err:
            projects.preview_status(p.id, user=who, db=db)
        assert err.value.status_code == 404
    with pytest.raises(HTTPException) as err:
        run(projects.start_preview("proj_a", projects.PreviewBody(archive="a.zip"), user=other, db=db))
    assert err.value.status_code == 403

    class Req:
        method, headers, url = "GET", {}, type("U", (), {"query": ""})()

        async def body(self):
            return b""
    resp = run(projects.preview_proxy(p.id, p.key, "", Req()))
    assert resp.status_code == 200 and "sandbox" in resp.headers["content-security-policy"] and resp.headers["cache-control"] == "no-store"
    with pytest.raises(HTTPException):
        run(projects.preview_proxy(p.id, "wrong", "", Req()))


def test_starting_replaces_the_projects_old_preview_and_caps_how_many_run(db, world, monkeypatch):
    user, _ = world
    monkeypatch.setattr(pv, "_need_sandbox", lambda: None)
    monkeypatch.setattr(pv, "_find", lambda *a: (None, "Lumen", "proj_a"))
    started = []

    async def fake_run(preview, archive, agent_id):
        started.append(preview.id)
        await asyncio.sleep(0)
    monkeypatch.setattr(pv, "_run", fake_run)

    async def go():
        first = await pv.start("proj_a", user.id, "agent_c", "a.zip")
        old = pv._previews[first["id"]]
        second = await pv.start("proj_a", user.id, "agent_c", "a.zip")
        assert old.status == "stopped" and second["id"] != first["id"]
        for i in range(pv.MAX_PREVIEWS):
            p = pv.Preview(f"proj_{i}", "u", "x.zip")
            pv._previews[p.id] = p
        with pytest.raises(fb.BuildError, match="Too many"):
            await pv.start("proj_a", user.id, "agent_c", "a.zip")
    run(go())


# ── a coding session ─────────────────────────────────────────────────

from core.models import DocumentRecord, Message
from core.usage import reset_usage_user, set_usage_user
from services import build_prompt as bp, chat_rows


@pytest.fixture
def me(db, world):
    user, _ = world
    ctx = set_usage_user(user.id, "proj_a")
    yield user
    reset_usage_user(ctx)


def add_build(db, name="lumen-first-build-20261029-100000.zip"):
    db.add(DocumentRecord(filename=name, agent_id="agent_c", project_id="proj_a", user_id="user_a", created_by="user_a", shared_with=[], title="Lumen: first build (code)"))
    db.commit()


def session(project="proj_a", user="user_a"):
    p = running(project)
    p.user_id, p.session, p.agent_id = user, True, "agent_c"
    return p


def test_a_session_needs_a_saved_build_and_says_what_to_do(db, me):
    assert run(pv.start_for_bot("agent_c")).startswith("Error: there is no saved build of this project to code on yet")


def test_starting_a_session_puts_a_card_in_the_chat_and_tells_the_bot_what_next(db, me, monkeypatch):
    add_build(db)
    calls = []

    async def fake_start(project_id, user_id, agent_id, archive, session=False):
        calls.append((project_id, archive, session))
        return {"id": "pid1"}
    monkeypatch.setattr(pv, "start", fake_start)
    out = run(pv.start_for_bot("agent_c"))
    assert calls == [("proj_a", "lumen-first-build-20261029-100000.zip", True)] and out.startswith("Started: a live coding session") and "change_code" in out
    card = json.loads(db.query(Message).filter(Message.agent_id == "agent_c").one().content[len("[[SOURCES:"):-2])
    assert card["kind"] == "session" and card["preview_id"] == "pid1" and card["title"] == "Coding session" and card["status"] == "running"


def test_a_change_needs_a_running_session_of_the_same_person_and_one_at_a_time(db, me, monkeypatch):
    assert run(pv.change_for_bot("agent_c", "make it blue")).startswith("Error: there is no coding session running")
    p = session()
    p.status = "starting"
    assert "still starting" in run(pv.change_for_bot("agent_c", "make it blue"))
    p.status = "running"
    assert run(pv.change_for_bot("agent_c", "  ")).startswith("Error: say what to change")
    spawned = []
    monkeypatch.setattr(pv.runs, "spawn", lambda coro: (spawned.append(coro), coro.close()))
    assert run(pv.change_for_bot("agent_c", "make it blue")).startswith("Started: Muse is making that change") and p.busy and len(spawned) == 1
    assert "still making the previous change" in run(pv.change_for_bot("agent_c", "and red"))
    p.user_id = "someone_else"  # a session is its starter's: another person cannot drive it
    assert run(pv.change_for_bot("agent_c", "x")).startswith("Error: there is no coding session")


class Muse:
    def __init__(self, fail=None):
        self.prompts, self.fail = [], fail

    async def build(self, prompt):
        self.prompts.append(prompt)
        if self.fail:
            raise RuntimeError(self.fail)
        return "Changed the header. Ran pytest: 5 passed."


def test_a_finished_change_updates_its_card_frees_the_session_and_tells_the_bot(db, me, monkeypatch):
    told = []
    monkeypatch.setattr("routes.chat._start_kickoff", lambda *a, **k: told.append((a, k)))
    p = session()
    p.box, p.busy = Muse(), True
    row = chat_rows.write_row("agent_c", "user_a", pv._card("Lumen", p.id, "running", "Changing: blue"))
    run(pv._change_and_tell(p, "agent_c", "Lumen", "make the header blue", row))
    assert not p.busy and "make the header blue" in p.box.prompts[0] and "do not start, stop or restart the server" in p.box.prompts[0]
    assert json.loads(db.get(Message, row).content[len("[[SOURCES:"):-2])["status"] == "done"
    assert told[0][1]["no_tools"] is True and "the frame has reloaded" in told[0][0][3] and "Changed the header" in told[0][0][3]

    p.box, p.busy = Muse(fail="the build took too long and was stopped"), True
    run(pv._change_and_tell(p, "agent_c", "Lumen", "x", row))
    assert not p.busy and json.loads(db.get(Message, row).content[len("[[SOURCES:"):-2])["status"] == "failed" and "did not finish: the build took too long" in told[1][0][3]


def test_the_session_tools_exist_and_none_of_them_asks_for_an_allow():
    from tools import catalog, outbox
    names = {t["name"] for t in catalog.local_tools()}
    assert {"start_coding_session", "change_code", "save_coding_session"} <= names
    assert not {"start_coding_session", "change_code", "save_coding_session"} & set(outbox._LOCAL_OUTWARD)
    assert "SessionBox" in pv.SessionBox.__name__ and pv.SessionBox.lifetime > pv.PreviewBox.lifetime


def test_the_dispatcher_routes_the_session_tools(monkeypatch):
    from tools import dispatch
    seen = []

    async def start(agent_id):
        seen.append(("start", agent_id))
        return "S"

    async def change(agent_id, request):
        seen.append(("change", request))
        return "C"
    monkeypatch.setattr(pv, "start_for_bot", start)
    monkeypatch.setattr(pv, "change_for_bot", change)
    assert run(dispatch.run_tool("start_coding_session", {}, "", "", "agent_c")) == "S"
    assert run(dispatch.run_tool("change_code", {"request": "blue"}, "", "", "agent_c")) == "C" and seen == [("start", "agent_c"), ("change", "blue")]


def test_saving_a_session_stores_the_code_without_our_rules_and_offers_the_pull_request(db, me, monkeypatch, tmp_path):
    monkeypatch.setattr("tools.documents.DOC_DIR", tmp_path / "docs")
    monkeypatch.setattr("tools.documents._r2", lambda: None)
    (tmp_path / "docs").mkdir()
    p = session()

    class Box:
        fs = {"AGENTS.md": bp.with_rules("# Ours\n"), "RUN.md": "# Run\n```\npython app.py\n```\n" + "x" * 200, "app.py": "x"}
        exported_with = None

        async def read(self, path):
            return self.fs.get(path)

        async def write(self, path, text):
            self.fs[path] = text

        async def files(self):
            return ["app.py", "RUN.md"]

        async def export(self):
            self.exported_with = self.fs["AGENTS.md"]
            return b"PKzip"
    p.box = Box()
    from core.models import Project
    db.get(Project, "proj_a").repo = "acme/lumen"
    db.commit()
    out = run(pv.save(p))
    assert out["files"] == 2 and out["zip"].endswith(".zip") and p.box.exported_with == "# Ours\n"  # the zip has the project's own rules, not ours
    assert "Build rules" in p.box.fs["AGENTS.md"]  # and the running session has its rules back
    from core.models import Task
    assert db.query(Task).filter(Task.title.like("Approve: pull request%")).count() == 1


def test_when_muse_cannot_say_how_to_start_the_projects_own_files_do():
    node = {"package.json": json.dumps({"scripts": {"dev": "next dev", "start": "next start"}})}
    assert pv.guess_recipe(node) == {"install": "npm install --no-audit --no-fund", "start": f"npm run dev -- --host 0.0.0.0 --port {pv.PREVIEW_PORT}", "vite": False}
    assert pv.guess_recipe({"package.json": json.dumps({"scripts": {"start": "node s.js"}})})["start"] == "npm run start"
    assert pv.guess_recipe({"requirements.txt": "flask", "app.py": "x"}) == {"install": "pip install -r requirements.txt", "start": "python app.py"}
    assert pv.guess_recipe({"package.json": "not json"}) is None and pv.guess_recipe({"README.md": "x"}) is None


def test_a_preview_reads_the_files_first_and_only_asks_muse_when_they_do_not_say(db, me, monkeypatch):
    p = pv.Preview("proj_a", "user_a", "a.zip")
    answers = []
    asked = []

    class Box:
        def __init__(self, *a):
            self.logs = []

        async def __aenter__(self):
            return self

        async def __aexit__(self, *e):
            return None

        async def inspect(self, prompt, schema, what="review"):
            asked.append(what)
            a = answers.pop(0)
            if isinstance(a, Exception):
                raise a
            return a

        async def install(self, cmd):
            raise RuntimeError("stop here: the recipe was reached")
    monkeypatch.setattr(pv, "PreviewBox", Box)

    async def extract(*a, **k):
        return {"requirements.txt": "flask", "app.py": "x"}
    monkeypatch.setattr(pv, "_extract", extract)
    run(pv._run(p, "a.zip", "agent_c"))
    assert asked == [] and p.stage == "stop here: the recipe was reached"  # got as far as installing, from the files alone, without asking Muse


def test_the_sessions_card_follows_its_state_instead_of_saying_starting_for_good(db, me):
    p = session()
    p.project_name = "Lumen"
    p.card_row = chat_rows.write_row("agent_c", "user_a", pv._card("Lumen", p.id, "running", "Coding session", "Starting"))
    card = lambda: json.loads(db.get(Message, p.card_row).content[len("[[SOURCES:"):-2])
    run(pv._sync_card(p))
    assert card()["status"] == "running" and "Running" in card()["stage"]
    p.status, p.stage = "failed", "the install failed; see the log"
    run(pv._sync_card(p))
    assert card()["status"] == "failed" and card()["stage"] == "the install failed; see the log"
    p.status = "stopped"
    run(pv._sync_card(p))
    assert card()["status"] == "done" and card()["stage"] == "The session ended."


# ── decisions inline, and the session that follows a build ───────────

def test_a_decision_is_asked_in_the_chat_as_well_as_on_the_board(db, me):
    from board.decisions import MAX_PENDING_DECISIONS  # noqa: F401
    from tools.board_actions import clean_options, run_request_decision
    text, event = run_request_decision("agent_c", "Draft the plan", "Which database?", db, ["Postgres (recommended)", "SQLite", "  ", "x" * 200, "a", "b"])
    assert event["type"] == "task_decision" and "Decisions pending" in text
    card = json.loads(db.query(Message).filter(Message.agent_id == "agent_c").one().content[len("[[SOURCES:"):-2])
    assert card["kind"] == "decision" and card["question"] == "Which database?" and card["title"] == "Draft the plan"
    assert card["options"] == ["Postgres (recommended)", "SQLite", "x" * 80, "a"]  # blanks dropped, long ones cut, four at most
    assert clean_options(None) == [] and clean_options("Postgres") == []
    run_request_decision("agent_c", "Other", "Open question?", db)
    assert json.loads(db.query(Message).filter(Message.agent_id == "agent_c").order_by(Message.id.desc()).first().content[len("[[SOURCES:"):-2])["options"] == []


def test_answering_the_inline_decision_is_the_boards_decision(db, me, monkeypatch):
    from routes import tasks
    from core.models import Project, Task
    from tools.board_actions import run_request_decision
    user = me
    run_request_decision("agent_c", "Draft the plan", "Which database?", db, ["Postgres"])
    task = db.query(Task).filter(Task.title == "Draft the plan").one()
    kicked = []
    monkeypatch.setattr("routes.chat._start_kickoff", lambda *a, **k: kicked.append(a))
    out = run(tasks._apply_decision(task, "Postgres", user, db.get(Project, "proj_a"), db))
    assert out["status"] != "decision" and "Postgres" in kicked[0][3]  # the bot carries on with the answer, however it was given


def test_the_decision_tool_offers_options_and_both_callers_pass_them():
    from tools import catalog
    tool = next(t for t in catalog.local_tools() if t["name"] == "request_decision")
    assert tool["input_schema"]["properties"]["options"]["type"] == "array" and "options" not in tool["input_schema"]["required"]
    import inspect
    from agents import peer_run
    from routes import chat
    assert 'get("options")' in inspect.getsource(chat) and 'get("options")' in inspect.getsource(peer_run)


def test_a_finished_build_starts_a_coding_session_and_the_master_is_told(db, tmp_path, monkeypatch):
    import inspect
    from services import first_build as fb
    started = []

    async def fake_start_session(project_id, name, user_id, agent_id, archive):
        started.append((project_id, name, archive))
    monkeypatch.setattr(pv, "start_session", fake_start_session)
    src = inspect.getsource(fb._build_and_tell)
    assert "preview.start_session(project_id, name, user_id, agent_id, saved[\"zip\"])" in src and src.index("start_session") < src.index("_offer_pull_request")
    out = fb.handover("Lumen", {"files": ["a"], "guide": "g" * 300 + "```", "summary": "s", "session": True}, "", {"zip": "z.zip", "guide": "g.md", "qa": ""}, "")
    assert "live coding session has been started" in out and "live coding session" not in fb.handover("Lumen", {"files": ["a"], "guide": "g" * 300 + "```", "summary": "s"}, "", {"zip": "z", "guide": "g", "qa": ""}, "")


def test_the_dev_badge_shows_only_on_the_coding_bot_and_only_for_the_person_whose_session_it_is(db, world):
    from routes import projects
    from core.models import Agent
    user, other = world
    db.add(Agent(id="agent_x", name="Other bot", system_prompt="x", project_id="proj_a"))
    db.commit()
    none = {"preview_id": "", "status": ""}
    assert projects.dev_session("agent_c", user=user, db=db) == none  # no session yet
    p = session()
    assert projects.dev_session("agent_c", user=user, db=db) == {"preview_id": p.id, "status": "running"}
    assert projects.dev_session("agent_x", user=user, db=db) == none  # not the coding bot
    assert projects.dev_session("agent_c", user=other, db=db) == none  # not a member
    assert projects.dev_session("nope", user=user, db=db) == none
    p.user_id = "user_o"  # someone else's session on the same project is not theirs to see
    assert projects.dev_session("agent_c", user=user, db=db) == none
    q = running()
    q.session = False  # a plain preview is not a coding session
    assert projects.dev_session("agent_c", user=user, db=db) == none
    p.user_id, p.status = "user_a", "stopped"
    assert projects.dev_session("agent_c", user=user, db=db) == none  # and it goes when the session does


# ── the live build: first, and where a review's fixes are made ───────

def test_every_prompt_asks_for_a_responsive_first_build():
    from services import build_prompt as bp, first_plan
    assert "viewport" in bp.RULES and "360, 768 and 1280" in bp.RULES and "44 px" in bp.RULES
    assert "responsive" in bp.build_prompt("X", "g", False).lower() and "360 px" in bp.build_prompt("X", "g", False)
    assert "Responsiveness is its own check" in bp.review_prompt("X", False) and "viewport meta" in bp.review_prompt("X", True)
    assert "phone, a tablet and a desktop" in first_plan.brief("X", "g", "", "", [], "Pat")


def test_a_session_takes_its_baseline_once_its_code_is_in_so_later_changes_can_be_measured(db, me, monkeypatch):
    p = pv.Preview("proj_a", "user_a", "a.zip")
    p.session = True
    calls = []

    class Box:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *e):
            return None

        async def baseline(self):
            calls.append("baseline")

        async def inspect(self, *a, **k):
            calls.append("recipe")
            raise RuntimeError("stop here")

        async def install(self, c):
            calls.append("install")
            raise RuntimeError("stop here")

        def __init__(self, *a):
            pass
    monkeypatch.setattr(pv, "SessionBox", Box)

    async def extract(*a, **k):
        calls.append("extract")
        return {"requirements.txt": "flask", "app.py": "x"}
    monkeypatch.setattr(pv, "_extract", extract)
    run(pv._run(p, "a.zip", "agent_c"))
    assert calls[:3] == ["extract", "baseline", "install"]  # the baseline is before anything is installed or changed


def test_review_fixes_are_made_in_the_live_session_when_there_is_one(db, me, monkeypatch):
    from services import pr_fix
    import json as _json
    told = []
    monkeypatch.setattr("routes.chat._start_kickoff", lambda *a, **k: told.append(a[3]))
    s = session()
    prompts = []

    class Muse:
        fs = {}

        async def write(self, path, text):
            self.fs[path] = text

        async def build(self, prompt):
            prompts.append(prompt)
            return "Fixed finding 1 and ran pytest."

        async def patch(self):
            return "diff --git a/app.py b/app.py\n--- a/app.py\n+++ b/app.py\n@@ -1 +1 @@\n-x\n+y\n"
    s.box = Muse()
    monkeypatch.setattr(pr_fix, "_offer_push", lambda *a: None)
    monkeypatch.setattr(pr_fix, "_save", lambda *a: "pr-7-fixes-1.zip")

    async def never(*a, **k):
        raise AssertionError("must not start a second sandbox")
    monkeypatch.setattr(pr_fix, "fix", never)
    pr = {"number": 7, "title": "Add shifts", "head": {"ref": "feature", "sha": "abc"}}
    pill = chat_rows.write_row("agent_c", "user_a", pr_fix._card("acme/lumen", pr, "running"))
    run(pr_fix._fix_and_tell("proj_a", "user_a", "acme/lumen", {"agent_id": "agent_c", "findings": "1. [MAJOR] x", "pr": pr, "project": "Lumen"}, pill))
    assert Muse.fs[".neuron/FINDINGS.md"] == "1. [MAJOR] x" and "live coding session" in prompts[0] and "do not start, stop or restart the server" in prompts[0]
    assert not s.busy and s.stage == "Running"  # the session is free for the next change
    assert "in the live build" in told[0]


def test_a_busy_session_is_not_driven_twice_and_without_a_session_the_old_sandbox_is_used(db, me, monkeypatch):
    from services import pr_fix
    s = session()
    s.busy = True
    with pytest.raises(RuntimeError, match="busy"):
        run(pr_fix.fix_live(s, "acme/lumen", {"number": 7}, "f", lambda stage: asyncio.sleep(0)))
    pv._previews.clear()
    used = []

    async def sandbox(repo, uid, pr, findings, progress):
        used.append("sandbox")
        return {"patch": "", "summary": "nothing"}
    monkeypatch.setattr(pr_fix, "fix", sandbox)
    monkeypatch.setattr("routes.chat._start_kickoff", lambda *a, **k: None)
    pr = {"number": 7, "title": "t", "head": {"ref": "f", "sha": "a"}}
    pill = chat_rows.write_row("agent_c", "user_a", pr_fix._card("acme/lumen", pr, "running"))
    run(pr_fix._fix_and_tell("proj_a", "user_a", "acme/lumen", {"agent_id": "agent_c", "findings": "f", "pr": pr, "project": "Lumen"}, pill))
    assert used == ["sandbox"]


# ── routes and requests inside the sandboxed frame ───────────────────

def test_the_shim_keeps_client_side_routes_and_links_inside_the_preview():
    out = pv.rewrite_html("<head></head><body></body>", "/api/preview/i/k/")
    for needle in ("history[n]", "'pushState','replaceState'", "document.addEventListener('click'", "a[href]", "window.fetch", "XMLHttpRequest.prototype.open"):
        assert needle in out, needle


def test_the_frames_requests_are_allowed_across_origins_and_the_browsers_questions_answered_without_asking_the_app(db, world):
    from routes import projects
    p = running()

    class Req:
        def __init__(self, method, headers=None):
            self.method, self.headers, self.url = method, headers or {}, type("U", (), {"query": ""})()

        async def body(self):
            return b""
    resp = run(projects.preview_proxy(p.id, p.key, "api/x", Req("GET")))
    assert resp.headers["access-control-allow-origin"] == "*" and "sandbox" in resp.headers["content-security-policy"]
    seen.clear()
    pre = run(projects.preview_proxy(p.id, p.key, "api/x", Req("OPTIONS", {"access-control-request-headers": "content-type"})))
    assert pre.status_code == 204 and pre.headers["access-control-allow-origin"] == "*" and pre.headers["access-control-allow-headers"] == "content-type"
    assert seen == []  # the app never saw the preflight
    from fastapi import HTTPException
    with pytest.raises(HTTPException):  # and a wrong key gets nothing, preflight included
        run(projects.preview_proxy(p.id, "wrong", "api/x", Req("OPTIONS")))


def test_a_first_build_runs_in_the_session_box_and_the_app_comes_up_while_it_is_written(db, me, monkeypatch):
    monkeypatch.setattr(pv, "BRING_UP_EVERY", 0)
    order = []

    class Box:
        def __init__(self, *a):
            self.written = {}

        async def __aenter__(self):
            return self

        async def __aexit__(self, *e):
            order.append("closed")

        async def files(self):
            return list(self.written)

        async def read(self, path):
            return self.written.get(path)

        async def write(self, path, text):
            order.append(f"write {path}")

        async def baseline(self):
            order.append("baseline")

        async def fingerprint(self):
            return "f"
    monkeypatch.setattr(pv, "LiveBuildBox", Box)

    async def launch(preview, box, recipe):
        order.append(f"launch {recipe['start']}")
        preview.status = "running"

        class Proc:
            class poll:
                @staticmethod
                async def aio():
                    return None

        return Proc(), asyncio.get_event_loop().create_future()

    async def hold(*a):
        order.append("hold")
    monkeypatch.setattr(pv, "_launch", launch)
    monkeypatch.setattr(pv, "_hold", hold)
    p = pv.Preview("proj_a", "user_a", "Lumen")
    p.session, p.busy = True, True

    async def steps(box):
        box.written["frontend/package.json"] = json.dumps({"scripts": {"dev": "vite"}})  # Muse has written enough to start
        await asyncio.sleep(0.05)
        order.append("built")
        return {"archive": b"z"}

    async def go():
        result = await pv.run_live(p, steps)
        await asyncio.wait_for(p.task, 5)
        return result
    assert run(go()) == {"archive": b"z"}
    assert [o for o in order if not o.startswith("write")] == ["launch cd frontend && npm run dev -- --host 0.0.0.0 --port 8080", "built", "baseline", "hold", "closed"]  # up before the build ended, kept as the session after
    assert p.busy is False


def test_a_failed_first_build_fails_its_session_too(db, me, monkeypatch):
    class Box:
        def __init__(self, *a):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *e):
            return None
    monkeypatch.setattr(pv, "LiveBuildBox", Box)
    monkeypatch.setattr(pv, "BRING_UP_EVERY", 0)
    p = pv.Preview("proj_a", "user_a", "Lumen")

    async def steps(box):
        raise RuntimeError("Muse stopped before the build was finished")
    with pytest.raises(RuntimeError, match="Muse stopped"):
        run(pv.run_live(p, steps))
    assert p.status == "failed"


def test_a_vite_project_is_started_under_the_previews_own_path_and_asked_for_there():
    recipe = pv.guess_recipe({"package.json": json.dumps({"scripts": {"dev": "vite --host 0.0.0.0"}})})
    assert recipe["vite"] is True
    p = pv.Preview("proj_a", "user_a", "Lumen")
    p.session, p.url, p.status = True, "https://ta-8080.w.modal.host", "running"
    seen = []

    class Client:
        async def request(self, method, url, **k):
            seen.append((url, k["headers"]["host"]))
            return httpx.Response(200, headers={"content-type": "text/html"}, content=b'<head></head><script src="' + pv.base_of(p).encode() + b'src/main.tsx">')
    p.client = Client()
    p.based = True
    status, headers, body = run(pv.forward(p, "GET", "src/main.tsx", "", {}, b""))
    assert seen == [(f"https://ta-8080.w.modal.host/api/preview/{p.id}/{p.key}/src/main.tsx", "localhost:8080")]  # the app is asked under its base, as a local page
    assert body.count(pv.base_of(p).encode() + b"src/main.tsx") == 1  # and its own links are not given the base a second time


def test_the_frame_is_given_in_memory_storage_for_apps_that_read_it_while_rendering():
    html = pv.rewrite_html("<html><head></head><body></body></html>", "/api/preview/x/k/")
    assert "localStorage" in html and "sessionStorage" in html and html.index("localStorage") < html.index("</head>")
