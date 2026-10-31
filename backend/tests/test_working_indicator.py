"""A bot working on its own (a brief the system gave it, an answer to a decision, a teammate asking) shows as typing, with what it is doing."""
import json

import pytest

from core.access import hash_password
from core.models import Agent, Project, ProjectMember, User
from routes import people
from services import runs


def ev(**kw):
    return "data: %s\n\n" % json.dumps(kw)


def run_with(*chunks, done=False):
    r = runs.Run("x")
    r.chunks, r.done = list(chunks), done
    return r


@pytest.fixture
def table(monkeypatch):
    """The registry of runs, empty, and nobody talking to a teammate."""
    monkeypatch.setattr(runs, "_runs", {})
    monkeypatch.setattr(runs, "talking", {})
    return runs._runs


def test_a_persons_runs_are_found_whatever_started_them(table):
    live, voice, bg = run_with(), run_with(), run_with()
    table.update({"a1:u1": live, "a2:voice:u1": voice, "a3:u1:bg": bg, "a4:u2": run_with(), "a5:u2:bg": run_with(), "a6:u10": run_with(), "a7:u1": run_with(done=True)})
    assert runs.running_for("u1") == {"a1": live, "a2": voice, "a3": bg}  # not u2's, not "u10" (a longer id), not one that has finished
    assert runs.running_for("") == {} and runs.running_for("nobody") == {}


def test_what_a_run_is_doing_comes_from_the_newest_thing_it_said():
    assert runs.doing(run_with()) == ""
    assert runs.doing(run_with(ev(type="status", message="Thinking…"))) == "Thinking…"
    started = run_with(ev(type="status", message="Thinking…"), ev(type="tool", name="create_document"))
    assert runs.doing(started) == "Using create document…"
    assert runs.doing(run_with(ev(type="tool", name="ask_teammate", who="Ada"))) == "Asking Ada…"
    assert runs.doing(run_with(ev(type="tool", name="ask_teammate"))) == "Asking a teammate…"


def test_a_tool_that_has_finished_is_not_still_being_used():
    chunks = [ev(type="status", message="Writing the answer…"), ev(type="tool", name="add_board_task"), ev(type="tool", name="add_board_task", ok=True)]
    assert runs.doing(run_with(*chunks)) == "Writing the answer…"  # falls back to the status line before it
    chunks += [ev(type="tool", name="calendar_add"), ev(type="tool", name="calendar_add", ok=False), ": ping\n\n", "garbage", "data: [1]\n\n", "data: {not json}\n\n"]
    assert runs.doing(run_with(*chunks)) == "Writing the answer…"
    two = [ev(type="tool", name="read_document"), ev(type="tool", name="web_search"), ev(type="tool", name="web_search", ok=True)]
    assert runs.doing(run_with(*two)) == "Using read document…"  # one of two parallel calls is over; the other is not


@pytest.fixture
def world(db):
    users = [User(id=f"u{i}", email=f"u{i}@example.com", name=f"U{i}", password_hash=hash_password("testpass123"), plan="free") for i in (1, 2)]
    here, there = Project(id="proj_here", name="Here"), Project(id="proj_there", name="There")
    db.add_all(users + [here, there])
    db.commit()
    db.add_all([ProjectMember(project_id="proj_here", user_id="u1", role="owner"), ProjectMember(project_id="proj_here", user_id="u2", role="member")])
    for aid, pid in (("a_planner", "proj_here"), ("a_mate", "proj_here"), ("a_other", "proj_there")):
        db.add(Agent(id=aid, name=aid, system_prompt="x", project_id=pid))
    db.commit()
    return users[0], here


def typing(db, user, project):
    return people.get_active_typing(user=user, project=project, db=db)


def test_a_bot_working_for_the_person_is_typing_and_says_what_it_is_doing(db, world, table):
    user, project = world
    assert typing(db, user, project) == {"people": [], "agents": []}  # nothing running: the answer is what it always was
    table["a_planner:u1:bg"] = run_with(ev(type="tool", name="ask_teammate", who="Grace"))
    got = typing(db, user, project)
    assert got["agents"] == ["a_planner"] and got["status"] == {"a_planner": "Asking Grace…"}


def test_only_this_projects_bots_and_this_persons_runs_are_shown(db, world, table):
    user, project = world
    table.update({"a_other:u1": run_with(), "a_mate:u2:bg": run_with(ev(type="status", message="Secret work"))})  # a bot of another project; another person's turn
    assert typing(db, user, project) == {"people": [], "agents": []}


def test_a_bot_in_a_conversation_with_a_teammate_is_working_too_though_it_has_no_status(db, world, table, monkeypatch):
    user, project = world
    monkeypatch.setattr(runs, "talking", {"a_mate": 1, "a_other": 1})
    got = typing(db, user, project)
    assert got["agents"] == ["a_mate"] and got["status"] == {}  # not the other project's, and nothing to say about what it is doing


def test_a_bot_typing_in_a_people_chat_and_one_working_alone_are_both_listed_once(db, world, table, monkeypatch):
    user, project = world
    people.typing_store.clear()
    people.typing_store.add_bot_typing(("proj_here", "u1", "u2", "a_planner"))
    table["a_planner:u1"] = run_with(ev(type="status", message="Thinking…"))
    try:
        got = typing(db, user, project)
    finally:
        people.typing_store.clear()
    assert got["agents"] == ["a_planner"] and got["status"] == {"a_planner": "Thinking…"}
