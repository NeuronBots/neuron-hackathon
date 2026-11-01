"""The planning flow on its own: modes that spend less, instructions read from files at every run, a preview that costs nothing, a usage meter."""
import asyncio
import shutil
from datetime import datetime, timedelta

import pytest
from fastapi import HTTPException

from core.models import Agent, DocumentRecord, Message, Project, ProjectMember, Task, UsageEvent, User
from routes import flows
from services import chat_rows, first_plan


def _brief(**kw):
    return first_plan.brief("Shop", "sell candles", "", "", [("Amber", "Ops", "orders")], "Tanay", **kw)


def test_the_default_brief_researches_on_the_web_and_asks_up_to_five_questions():
    text = _brief()
    assert "ON THE WEB" in text and "ask at most five in all" in text and "Amber (Ops, orders)" in text and "Name: Shop" in text


def test_each_research_mode_changes_what_the_team_is_asked_to_do():
    assert "Do NOT use web_search" in _brief(research="light") and "SHORT answer" in _brief(research="light")
    assert "Skip research" in _brief(research="off") and "ON THE WEB" not in _brief(research="off")
    reuse = _brief(research="reuse", docs=[("Research: market", "r1.md")])
    assert "Reuse the research" in reuse and "- Research: market (file r1.md)" in reuse
    assert "Reuse the research" not in _brief(research="reuse") and "Research lightly" in _brief(research="reuse")  # nothing saved: a short pass instead
    assert "ON THE WEB" in _brief(research="nonsense")  # an unknown mode is the default


def test_the_number_of_questions_is_a_dial_down_to_none():
    assert "at most two in all" in _brief(decisions=2)
    none = _brief(decisions=0)
    assert "Take no decisions" in none and "request_decision. Settle" in none and "request_decision with the title" not in none
    assert "at most five in all" in _brief(decisions=99)  # clamped


def test_the_environment_sets_what_planning_spends_by_default(monkeypatch):
    monkeypatch.setenv("PLAN_RESEARCH", "off")
    monkeypatch.setenv("PLAN_MAX_DECISIONS", "1")
    assert first_plan.defaults() == ("off", 1) and "Skip research" in _brief() and "at most one in all" in _brief()
    monkeypatch.setenv("PLAN_RESEARCH", "bogus")
    monkeypatch.setenv("PLAN_MAX_DECISIONS", "many")
    assert first_plan.defaults() == ("full", 5)


def test_an_edit_to_a_flow_file_is_used_by_the_next_run_with_no_restart(tmp_path, monkeypatch):
    shutil.copytree(first_plan.FLOWS_DIR, tmp_path / "flows")
    monkeypatch.setattr(first_plan, "FLOWS_DIR", tmp_path / "flows")
    assert "PLEASE BE BRIEF" not in _brief()
    plan = tmp_path / "flows" / "plan.md"
    plan.write_text(plan.read_text().replace("Start now.", "PLEASE BE BRIEF. Start now."))
    assert "PLEASE BE BRIEF" in _brief()
    plan.write_text("Project {name} {nonsense} and a literal {{brace}}. {research}")  # an unknown placeholder stays as written; doubled braces are literal
    assert _brief(research="off").startswith("Project Shop {nonsense} and a literal {brace}. 3. Skip research")


def _owner_project(db):
    user = User(id="u", name="Tanay", email="t@x.io", password_hash="x")
    project = Project(id="p", name="Shop", goal="sell candles")
    db.add_all([user, project])
    db.flush()
    db.add_all([Agent(id="m", name="Master", system_prompt="x", project_id="p", is_master="true"), Agent(id="a", name="Amber", system_prompt="x", project_id="p", label="Ops", field="orders"),
                ProjectMember(project_id="p", user_id="u", role="owner")])
    db.commit()
    return project, user


def test_the_preview_shows_the_brief_for_the_project_and_costs_nothing(db):
    project, user = _owner_project(db)
    db.add(DocumentRecord(filename="r1.md", title="Research: market", project_id="p", agent_id="a"))
    db.commit()
    out = flows.plan_preview("reuse", 2, project, user, db)
    assert out["research"] == "reuse" and out["decisions"] == 2 and out["reusable_docs"] == 1 and "Research: market" in out["text"] and "Amber (Ops, orders)" in out["text"]
    assert out["words"] > 300 and out["tokens"] > out["words"]


def test_a_flow_file_being_edited_into_a_broken_state_is_reported_not_crashed(db, tmp_path, monkeypatch):
    project, user = _owner_project(db)
    (tmp_path / "plan.md").write_text("broken {")
    monkeypatch.setattr(first_plan, "FLOWS_DIR", tmp_path)
    with pytest.raises(HTTPException) as e:
        flows.plan_preview("", -1, project, user, db)
    assert e.value.status_code == 422 and "backend/flows" in e.value.detail


def test_only_the_owner_can_use_the_flows(db):
    project, user = _owner_project(db)
    db.query(ProjectMember).delete()
    db.add(ProjectMember(project_id="p", user_id="u", role="member"))
    db.commit()
    with pytest.raises(HTTPException) as e:
        flows.plan_preview("", -1, project, user, db)
    assert e.value.status_code == 403


def test_running_planning_starts_the_master_once_on_the_chosen_brief(db, monkeypatch):
    project, user = _owner_project(db)
    started = []

    async def fake_start(master_id, project_id, user_id, text):
        asyncio.get_running_loop()
        started.append((master_id, project_id, user_id, text))

    monkeypatch.setattr(first_plan, "start", fake_start)
    out = asyncio.run(flows.plan_run(flows.PlanRun(research="off", decisions=0), project, user, db))
    assert out["started"] and out["research"] == "off" and out["decisions"] == 0
    assert started[0][0] == "m" and "Skip research" in started[0][3] and "Take no decisions" in started[0][3]
    asyncio.run(flows.plan_run(flows.PlanRun(), project, user, db))
    assert db.query(Task).filter(Task.title == first_plan.TITLE).count() == 1 and len(started) == 2  # the board task is not duplicated


def test_the_usage_meter_adds_up_what_the_bots_spent_since_a_moment(db):
    project, user = _owner_project(db)
    now = datetime.utcnow()
    db.add_all([UsageEvent(project_id="p", agent_id="m", input_tokens=1000, output_tokens=200, cache_read_tokens=50, created_at=now),
                UsageEvent(project_id="p", agent_id="m", input_tokens=500, output_tokens=100, created_at=now),
                UsageEvent(project_id="p", agent_id="a", input_tokens=300, output_tokens=40, created_at=now),
                UsageEvent(project_id="p", agent_id="a", input_tokens=9999, output_tokens=9999, created_at=now - timedelta(hours=2)),  # before the run
                UsageEvent(project_id="other", agent_id="m", input_tokens=7777, output_tokens=7777, created_at=now)])
    db.commit()
    since = (now - timedelta(minutes=5)).isoformat() + "Z"
    out = flows.usage(since, project, user, db)
    assert (out["input"], out["output"], out["cache_read"], out["calls"]) == (1800, 340, 50, 3)
    assert [(b["name"], b["input"], b["calls"]) for b in out["bots"]] == [("Master", 1500, 2), ("Amber", 300, 1)]
    with pytest.raises(HTTPException):
        flows.usage("not a time", project, user, db)


# ── chat noise ───────────────────────────────────────────────────────────

def test_a_failed_turn_says_what_to_do_not_the_exception():
    from markers.display import friendly_failure
    busy = friendly_failure("TimeoutError: QueuePool limit of size 5 overflow 10 reached, connection timed out, timeout 8.00")
    assert "server was busy" in busy and "QueuePool" not in busy and "TimeoutError" not in busy
    assert "token limit" in friendly_failure("You've reached your 6-hour token limit.")
    assert friendly_failure("ValueError: the plan has no title") == "This didn't finish: the plan has no title"
    assert friendly_failure("") == "This didn't finish. Ask again and it will pick it up."


def test_an_email_approval_names_the_recipients_not_a_python_list():
    from tools.writes import describe
    one = describe("gmail_create_draft", {"to": ["tanay@neuronbot.in"], "subject": "Hi"})
    assert one.splitlines()[0] == "Create draft to tanay@neuronbot.in"
    two = describe("gmail_create_draft", {"to": ["a@x.io", "b@x.io"]})
    assert two.splitlines()[0].endswith("to a@x.io, b@x.io") and "[" not in two.splitlines()[0]
    assert describe("gmail_create_draft", {"to": "c@x.io"}).splitlines()[0].endswith("to c@x.io")


def test_status_lines_are_stored_as_cards_so_the_model_never_reads_them():
    from llm.history import history_for_model, is_card
    from services import chat_rows
    row = chat_rows.card("step", text="Reading the new order and teammate notes.")
    assert is_card(row) and '"kind": "step"' in row
    kept = history_for_model([{"role": "user", "content": "hi"}, {"role": "assistant", "content": row}, {"role": "assistant", "content": "Done."}])
    assert [m["content"] for m in kept] == ["hi", "Done."]


# ── what Neuron already knows ────────────────────────────────────────────

def test_every_bot_is_told_how_neuron_works_and_whether_payments_are_on(db):
    from prompts.system import platform_block
    project, _ = _owner_project(db)
    master, mate = db.get(Agent, "m"), db.get(Agent, "a")
    off = platform_block(master, db)
    assert "Payments are OFF for this project." in off and "add_payments" in off and "Never ask for a Razorpay or Stripe account" in off
    project.payments_enabled = True
    db.commit()
    assert "Payments are ON for this project." in platform_block(mate, db)  # a teammate gets it too, not only the Master


def test_the_platform_facts_are_a_file_read_at_every_turn(db, monkeypatch, tmp_path):
    from prompts import system
    from prompts.system import platform_block
    project, _ = _owner_project(db)
    from pathlib import Path
    real = Path(system.__file__).resolve().parents[1] / "flows" / "platform.md"
    fake_root = tmp_path / "backend"
    (fake_root / "flows").mkdir(parents=True)
    (fake_root / "prompts").mkdir()
    (fake_root / "flows" / "platform.md").write_text("Hosting is {payments_state} ONE-LINE-EDIT")
    monkeypatch.setattr(system, "__file__", str(fake_root / "prompts" / "system.py"))
    assert platform_block(db.get(Agent, "m"), db) == "\n\nHosting is Payments are OFF for this project. ONE-LINE-EDIT"
    assert real.exists()


def test_add_payments_is_a_tool_the_owner_must_allow():
    from tools.catalog import local_tools
    from tools.outbox import is_outward
    from tools.writes import describe
    assert "add_payments" in {t["name"] for t in local_tools()} and is_outward("add_payments")
    assert "5%" in describe("add_payments", {}) and "Razorpay" in describe("add_payments", {})


def test_turning_payments_on_from_chat_needs_the_owner_a_build_and_happens_once(db, monkeypatch):
    from core.usage import reset_usage_user, set_usage_user
    from services import checkout, payments, runs
    project, user = _owner_project(db)
    started = []

    async def fake_add(project_id, user_id, agent_id):
        started.append((project_id, user_id, agent_id))

    monkeypatch.setattr(checkout, "add_checkout", fake_add)
    monkeypatch.setattr(runs, "spawn", lambda coro: asyncio.get_running_loop().create_task(coro))
    monkeypatch.setattr(payments, "has_build", lambda db, pid: False)

    async def go():
        a = await checkout.turn_on("m")  # no one signed in
        tok = set_usage_user("u", "p")
        try:
            no_build = await checkout.turn_on("m")
            monkeypatch.setattr(payments, "has_build", lambda db, pid: True)
            ok = await checkout.turn_on("m")
            again = await checkout.turn_on("m")
            db.query(ProjectMember).delete()
            db.add(ProjectMember(project_id="p", user_id="u", role="member"))
            db.commit()
            not_owner = await checkout.turn_on("m")
        finally:
            reset_usage_user(tok)
        await asyncio.sleep(0)
        return a, no_build, ok, again, not_owner

    a, no_build, ok, again, not_owner = asyncio.run(go())
    assert a.startswith("Error") and no_build.startswith("Error: there is no built site")
    assert ok.startswith("Payments are on") and "Do not ask for a payment page" in ok and started == [("p", "u", "m")]
    assert again.startswith("Payments are already on") and not_owner.startswith("Error: only the project owner")


def test_a_flow_leaves_a_heading_in_the_chat_the_model_never_reads(db):
    from core.models import Message
    from llm.history import history_for_model
    from services import chat_rows
    _owner_project(db)
    row = chat_rows.phase("m", "u", "plan", "The team researches.")
    assert row > 0
    stored = db.query(Message).filter(Message.id == row).one().content
    assert '"kind": "phase"' in stored and '"label": "Planning"' in stored and "The team researches." in stored
    assert history_for_model([{"role": "assistant", "content": stored}]) == []  # a card for the page, not something the model said
    assert chat_rows.phase("no-such-bot", "u", "build") == 0  # a heading never stops a flow
    assert set(chat_rows.PHASES) == {"plan", "build", "live", "orders", "payments"}


# ── where the project is, and what each stage cost ───────────────────────

def _phase(db, agent_id, phase, at):
    from core.models import Message
    from services import chat_rows
    db.add(Message(agent_id=agent_id, user_id="u", role="assistant", content=chat_rows.card("phase", phase=phase, label=phase.title(), note=""), created_at=at))


def test_each_heading_shows_the_time_and_tokens_until_the_next_one_began(db):
    project, user = _owner_project(db)
    t0 = datetime(2026, 11, 1, 10, 0, 0)
    _phase(db, "m", "plan", t0)
    _phase(db, "m", "build", t0 + timedelta(minutes=6))
    db.add_all([UsageEvent(project_id="p", agent_id="m", input_tokens=3000, output_tokens=500, created_at=t0 + timedelta(minutes=1)),
                UsageEvent(project_id="p", agent_id="a", input_tokens=1000, output_tokens=200, created_at=t0 + timedelta(minutes=5)),
                UsageEvent(project_id="p", agent_id="m", input_tokens=700, output_tokens=100, created_at=t0 + timedelta(minutes=8)),  # after the build began
                UsageEvent(project_id="other", agent_id="m", input_tokens=9999, output_tokens=9999, created_at=t0 + timedelta(minutes=2))])
    db.commit()
    plan, build = flows.phase_costs(db, project, user, now=t0 + timedelta(hours=1))
    assert (plan["phase"], plan["minutes"], plan["tokens"], plan["calls"], plan["running"]) == ("plan", 6.0, 4700, 2, False)
    assert (build["phase"], build["tokens"], build["calls"], build["running"]) == ("build", 800, 1, False)
    assert build["minutes"] == 2.0  # the newest one ends at its last spending, not at "now"
    live = flows.phase_costs(db, project, user, now=t0 + timedelta(minutes=8, seconds=30))[1]
    assert live["running"] is True and live["minutes"] == 2.5  # still spending: it runs up to now


def test_the_path_says_which_stage_the_project_is_at_and_what_comes_next(db, monkeypatch):
    from core.models import PriceItem, Routine
    project, user = _owner_project(db)
    monkeypatch.setattr(payments_mod := __import__("services.payments", fromlist=["x"]), "has_build", lambda db_, pid: False)
    out = flows.path(project, user, db)
    assert [(s["id"], s["state"]) for s in out["steps"]] == [("plan", "current"), ("build", "pending"), ("live", "pending"), ("orders", "pending"), ("payments", "pending")]
    assert out["steps"][0]["hint"].startswith("The Master researches")
    db.add(DocumentRecord(filename="plan.md", title="First build plan: Shop", project_id="p", agent_id="m"))
    db.commit()
    monkeypatch.setattr(payments_mod, "has_build", lambda db_, pid: True)
    project.site_slug = "shop-1"
    db.commit()
    states = {s["id"]: s["state"] for s in flows.path(project, user, db)["steps"]}
    assert states == {"plan": "done", "build": "done", "live": "done", "orders": "current", "payments": "pending"}
    db.add(Routine(agent_id="a", user_id="u", title="t", instruction="i", schedule='{"kind": "on_order"}'))
    project.payments_enabled = True
    db.commit()
    assert {s["id"]: s["state"] for s in flows.path(project, user, db)["steps"]}["payments"] == "current"  # on, but no prices yet
    db.add(PriceItem(project_id="p", sku="x", name="X", amount=10_000))
    db.commit()
    assert all(s["state"] == "done" for s in flows.path(project, user, db)["steps"])


def test_the_path_is_the_owners_only(db):
    project, user = _owner_project(db)
    db.query(ProjectMember).delete()
    db.add(ProjectMember(project_id="p", user_id="u", role="member"))
    db.commit()
    with pytest.raises(HTTPException) as e:
        flows.path(project, user, db)
    assert e.value.status_code == 403


# ── what Muse prints while it builds ─────────────────────────────────────

def test_a_builds_terminal_gives_new_lines_only_and_the_owner_alone(db):
    from services import build_log
    project, user = _owner_project(db)
    db.add(Message(id=77, agent_id="m", user_id="u", role="assistant", content=chat_rows.card("build", project="Shop", status="running", row=77)))
    db.commit()
    build_log.add(77, "npm install\n\nadded 5 packages\r")
    first = flows.build_log_lines(77, 0, user, db)
    assert first == {"lines": ["npm install", "added 5 packages"], "next": 2, "running": True}
    build_log.add(77, "writing index.html")
    assert flows.build_log_lines(77, 2, user, db)["lines"] == ["writing index.html"]
    assert flows.build_log_lines(77, 3, user, db)["lines"] == []
    other = User(id="x", name="X", email="x@x", password_hash="")
    with pytest.raises(HTTPException) as e:
        flows.build_log_lines(77, 0, other, db)
    assert e.value.status_code == 404


# ── the walkthrough written for the project ──────────────────────────────

def test_the_walkthrough_always_has_five_stages_and_only_clean_text():
    from services import flow_intro as fi
    base = fi.fallback("Wick & Wax", "A candle shop", [("Jasmine", "Design"), ("Amber", "Checkout")])
    assert [s["id"] for s in base["steps"]] == ["plan", "build", "live", "orders", "payments"] and "Jasmine, Amber" in base["steps"][0]["what"]
    got = fi.clean({"steps": [{"id": "plan", "what": "  Jasmine looks at   candles. ", "you": "x" * 500}, {"id": "nope", "what": "ignored"}],
                    "demo": {"question": "Scented or plain?", "options": ["Scented"], "price": True, "currency": "usd", "item": "Soy candle"}}, base)
    assert got["steps"][0]["what"] == "Jasmine looks at candles." and len(got["steps"][0]["you"]) == 140
    assert got["steps"][1] == base["steps"][1]  # missing: the plain one
    assert got["demo"]["options"] == base["demo"]["options"] and got["demo"]["price"] == 500 and got["demo"]["currency"] == "USD" and got["demo"]["item"] == "Soy candle"


def test_the_walkthrough_is_written_once_and_kept(db, monkeypatch):
    from services import flow_intro as fi
    project, user = _owner_project(db)
    calls = []

    async def fake(system, user_text, room, phase):
        calls.append(user_text)
        return {"steps": [{"id": "plan", "what": "Jasmine studies the candle shop.", "you": "Pick a scent."}]}

    monkeypatch.setattr("services.project_setup._ask", fake)
    first = asyncio.run(flows.flow_intro(project, user, db))
    again = asyncio.run(flows.flow_intro(project, user, db))
    assert first["steps"][0]["what"] == "Jasmine studies the candle shop." and again == first and len(calls) == 1
    assert "Project: " in calls[0] and "Team:" in calls[0]


def test_when_the_model_fails_the_plain_walkthrough_is_given_and_not_kept(db, monkeypatch):
    project, user = _owner_project(db)

    async def broken(*a, **k):
        raise RuntimeError("down")

    monkeypatch.setattr("services.project_setup._ask", broken)
    out = asyncio.run(flows.flow_intro(project, user, db))
    assert len(out["steps"]) == 5 and not project.flow_intro


# ── Muse's own tokens count toward the plan ──────────────────────────────

MUSE_RUN = "\n".join([
    '{"payload_type":"task.lifecycle.proposed","payload":{"event":{"task_kind":"tool.shell.exec"}}}',
    '{"payload_type":"task.lifecycle.proposed","payload":{"event":{"task_kind":"model.unknown.response"}}}',
    '{"payload_type":"run.output.delta","payload":{"text":"Writing src/App"}}',
    '{"payload_type":"run.output.delta","payload":{"text":".tsx\\nInstalling"}}',
    '{"payload_type":"run.usage","payload":{"models":[{"model":"m","input_tokens":1000,"cached_tokens":400,"output_tokens":200,"reasoning_tokens":50},{"model":"m2","input_tokens":10,"cached_tokens":0,"output_tokens":5,"reasoning_tokens":0}]}}',
    '{"payload_type":"run.terminal.completed","payload":{"text":"Built the shop."}}',
])


def test_muses_json_run_gives_its_closing_message_and_its_tokens():
    from services import muse_usage as mu
    text, tokens = mu.parse(MUSE_RUN)
    assert text == "Built the shop." and tokens == {"input": 1010, "cached": 400, "output": 255}
    assert mu.parse("plain text from an older muse") == ("plain text from an older muse", {"input": 0, "cached": 0, "output": 0})


def test_the_terminal_gets_whole_lines_and_the_tools_started_but_no_bookkeeping():
    from services import muse_usage as mu
    lines = []
    feed = mu.Feed(lines.append)
    for chunk in MUSE_RUN.split("\n") + ["stderr: warning"]:
        feed.feed(chunk)
    feed.close()
    assert lines == ["→ tool.shell.exec", "Writing src/App.tsx", "stderr: warning", "Installing"] or lines == ["→ tool.shell.exec", "Writing src/App.tsx", "Installing", "stderr: warning"]


def test_the_runs_tokens_are_recorded_for_the_person_whose_build_it_is(db, monkeypatch):
    from services import muse_usage as mu
    got = []
    monkeypatch.setattr("core.usage.record_usage", lambda model, usage, user_id=None, **kw: got.append((model, usage, user_id, kw)))
    mu.record({"input": 1010, "cached": 400, "output": 255}, "u")
    mu.record({"input": 0, "cached": 0, "output": 0}, "u")  # nothing to count
    assert got == [("muse", {"input_tokens": 1010, "output_tokens": 255, "cache_read_input_tokens": 400}, "u", {"agent_id": None, "kind": "muse"})]


def test_a_build_run_asks_for_json_returns_the_closing_message_and_counts_its_tokens(monkeypatch):
    from services import build_sandbox as bs, muse_usage as mu
    monkeypatch.setenv("META_API_KEY", "k")
    box, calls, counted, shown = bs.BuildBox("high", 60), [], [], []

    class FS:
        class write_text:
            @staticmethod
            async def aio(*a):
                return None

    box.sb = type("SB", (), {"filesystem": FS})()

    async def fake_exec(*args, **kw):
        calls.append((args, kw))
        for line in MUSE_RUN.split("\n"):
            kw["on_line"](line)
        return 0, MUSE_RUN, ""

    monkeypatch.setattr(box, "_exec", fake_exec)
    monkeypatch.setattr(mu, "record", lambda totals, owner=None, agent=None: counted.append((totals, owner)))
    box.owner, box.on_line = "u", shown.append
    assert asyncio.run(box.build("do it")) == "Built the shop."
    assert "--json" in calls[0][0] and counted == [({"input": 1010, "cached": 400, "output": 255}, "u")]
    assert shown[:2] == ["→ tool.shell.exec", "Writing src/App.tsx"]
