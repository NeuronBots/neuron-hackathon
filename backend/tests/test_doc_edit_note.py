"""A person's hand edit of a document is passed to the bot that wrote it."""
from types import SimpleNamespace

from core.models import Agent, Project, User, get_history
from core.usage import reset_usage_user, set_usage_user
from llm.history import history_for_model
from routes.misc import tell_bot_of_edit


def test_edit_becomes_a_hidden_note_the_model_sees(db):
    db.add(Project(id="p1", name="P"))
    db.add(User(id="u1", email="a@b.c", name="A", password_hash="x"))
    db.commit()
    agent = Agent(id="a1", name="Bot", project_id="p1", system_prompt="x")
    db.add(agent)
    db.commit()
    rec = SimpleNamespace(agent_id="a1", project_id="p1", title="Plan")
    tell_bot_of_edit(db, SimpleNamespace(id="u1"), rec, "plan.md", "# Plan\nship it\n", "# Plan\nship it later\n")
    token = set_usage_user("u1", "p1")
    try:
        history = get_history(agent)
    finally:
        reset_usage_user(token)
    assert history[-1].get("hidden") and "+ship it later" in history[-1]["content"]
    assert "ship it later" in history_for_model(history)[-1]["content"]
    tell_bot_of_edit(db, SimpleNamespace(id="u1"), rec, "plan.md", "same", "same")  # no change, no note
    assert db.query(Agent).count() == 1
