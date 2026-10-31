"""A new project starts with its team planning the first build."""
import asyncio
import re

import pytest
from fastapi import BackgroundTasks

from core.access import hash_password
from core.models import Agent, Task, User
from routes import projects
from services import first_plan
from tools import catalog

TEAM = [("Ada", "QA Architect", "testing"), ("Grace", "Design Lead", "")]


def text(**kw):
    args = dict(name="Lumen", goal="A health app for nurses", repo="acme/lumen", summary="A Flask API and a React client.", team=TEAM, person="Tanay")
    return first_plan.brief(**{**args, **kw})


def test_the_brief_tells_the_master_the_project_the_team_and_the_whole_job():
    t = text()
    for fact in ("Lumen", "A health app for nurses", "acme/lumen", "A Flask API and a React client.", "Tanay", "- Ada (QA Architect, testing)", "- Grace (Design Lead)"):
        assert fact in t
    for step in ("add_board_task", "read_document", "ask_teammate", "request_decision", "create_document", "complete_board_task"):
        assert step in t
    assert "ONE detailed plan" in t and "FIRST BUILD" in t and "recommendation" in t and "at most five" in t
    assert 'First build plan: Lumen' in t and "Do not write or push code" in t and "needs the person's approval" in t


def test_decisions_and_completion_use_the_boards_task_title_so_they_stay_attached_to_it():
    t = text()
    assert t.count(f'"{first_plan.TITLE}"') >= 3  # add, request_decision, complete_board_task


def test_with_no_repository_it_does_not_send_the_master_looking_for_one():
    t = text(repo="", summary="")
    assert "Repository: none linked yet" in t and "GitHub tools" not in t
    assert "GitHub tools" in text()


def test_with_no_goal_the_first_decision_is_the_goal_and_with_no_team_the_master_does_it_all():
    assert "not given: ask for it as your first decision" in text(goal="")
    assert "no teammates yet" in text(team=[])


def test_every_tool_the_brief_names_exists_for_the_master():
    names = {t["name"] for t in catalog.local_tools()} | {"ask_teammate"}  # ask_teammate is offered per agent, with the teammates' names
    used = set(re.findall(r"\b(add_board_task|read_document|ask_teammate|request_decision|create_document|complete_board_task|build_project)\b", text()))
    assert used == {"add_board_task", "read_document", "ask_teammate", "request_decision", "create_document", "complete_board_task", "build_project"}
    assert used <= names
    create = next(t for t in catalog.local_tools() if t["name"] == "create_document")
    assert "md" in str(create["input_schema"])


@pytest.fixture
def owner(db):
    user = User(id="user_fp", email="fp@example.com", name="Tanay", password_hash=hash_password("testpass123"), plan="free", is_admin=True)
    db.add(user)
    db.commit()
    return user


def create(db, owner, **kw):
    background = BackgroundTasks()
    body = projects.ProjectBody(name="Lumen", goal="A health app", repo="acme/lumen", summary="Flask and React.",
                                agents=[projects.SuggestedAgent(name="Ada", label="QA Architect", field="testing", system_prompt="Ada tests.")], **kw)
    return asyncio.run(projects.create_project(body, background, user=owner, db=db)), background


def test_a_new_project_gets_a_planning_task_on_the_masters_board_and_the_master_is_started(db, owner):
    out, background = create(db, owner)
    master = db.query(Agent).filter(Agent.project_id == out["id"], Agent.is_master == "true").one()
    task = db.query(Task).one()
    assert (task.agent_id, task.title, task.status) == (master.id, first_plan.TITLE, "working") and out["plan_started"] is True
    assert out["master_id"] == master.id  # so the app can open the chat where the planning happens
    (job,) = background.tasks
    assert job.func is first_plan.start and job.args[:3] == (master.id, out["id"], owner.id)
    brief = job.args[3]
    assert "A health app" in brief and "acme/lumen" in brief and "Ada (QA Architect, testing)" in brief and "Tanay" in brief


def test_the_person_can_decline_the_plan_and_then_nothing_is_started(db, owner):
    out, background = create(db, owner, start_plan=False)
    assert db.query(Task).count() == 0 and background.tasks == [] and out["plan_started"] is False
    assert db.query(Agent).filter(Agent.project_id == out["id"]).count() == 2  # the team is made all the same


def test_the_plan_is_the_default_so_a_client_that_says_nothing_gets_one():
    assert projects.ProjectBody(name="x").start_plan is True


def test_starting_hands_the_brief_to_the_master_as_a_background_job(monkeypatch):
    calls = []
    monkeypatch.setattr("routes.chat._start_kickoff", lambda *a, **k: calls.append((a, k)))
    asyncio.run(first_plan.start("master1", "proj1", "user1", "THE BRIEF"))
    assert calls == [(("master1", "Neuron", "proj1", "THE BRIEF", "user1"), {})]


def test_a_research_document_without_web_sources_is_refused_and_one_with_sources_is_saved(db, monkeypatch):
    import asyncio
    from tools import dispatch
    call = lambda content: asyncio.run(dispatch.run_tool("create_document", {"title": "Research: pricing", "format": "md", "filename": "r", "content": content}, "", "", ""))
    assert call("From memory, juice sells for 40.").startswith("Error: a research document must rest on the web")
    assert call("One source https://a.example/x only").startswith("Error")
    assert not call("Found https://a.example/x and https://b.example/y: prices vary.").startswith("Error: a research")
