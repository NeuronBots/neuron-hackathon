"""People with a field are the bots' humans in the loop: listed in the prompt, and a decision can be sent to one."""
from core.models import Agent, Project, ProjectMember, Task, TaskEvent, User
from core.usage import set_usage_user
from prompts.directory import build_people
from tools.board_actions import run_request_decision


def _team(db):
    db.add(Project(id="p1", name="P"))
    db.add_all([User(id="u1", email="ann@x.io", name="Ann", password_hash="x"), User(id="u2", email="bo@x.io", name="Bo", password_hash="x")])
    db.commit()
    db.add_all([ProjectMember(project_id="p1", user_id="u1", role="owner"), ProjectMember(project_id="p1", user_id="u2", field="legal and contracts")])
    db.add(Agent(id="a1", name="Bot", project_id="p1", system_prompt="x"))
    db.commit()


def test_prompt_lists_only_people_with_a_field(db):
    _team(db)
    text = build_people("a1", db)
    assert "- Bo: legal and contracts" in text and "Ann" not in text and "`to`" in text


def test_no_people_no_section(db):
    _team(db)
    db.query(ProjectMember).update({"field": ""})
    assert build_people("a1", db) == ""


def test_decision_goes_to_the_named_person(db, monkeypatch):
    _team(db)
    pushed = []
    set_usage_user("u1", "p1")  # the chat card is filed under the current person
    monkeypatch.setattr("tools.board_actions.notify_pending", lambda agent, q, db, only=None: pushed.append(only))
    run_request_decision("a1", "Sign NDA", "Sign it?", db, to="bo")
    assert any("for Bo (legal and contracts)" in (e.text or "") for e in db.query(TaskEvent).all())
    assert pushed == [["u2"]]
    run_request_decision("a1", "Pick a colour", "Which?", db, to="nobody")  # unknown name: everyone is told
    assert pushed[-1] is None
