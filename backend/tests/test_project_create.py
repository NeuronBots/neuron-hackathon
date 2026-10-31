"""Creating a project with a team: the Master is the coding bot, and a bot keeps the id its avatar was drawn from."""
import asyncio
import pytest

from core.access import hash_password
from fastapi import BackgroundTasks

from core.models import Agent, Project, User
from routes import projects


@pytest.fixture
def owner(db):
    user = User(id="user_owner", email="o@example.com", name="Owner", password_hash=hash_password("testpass123"), plan="free", is_admin=True)
    db.add(user)
    db.commit()
    return user


def bot(name):
    return projects.SuggestedAgent(name=name, label=f"{name} Lead", field="things", system_prompt=f"{name} does things.")


def make(db, owner, repo="acme/app", names=("Ada", "Grace", "Linus"), start_plan=False, background=None):
    body = projects.ProjectBody(name="Proj", goal="Ship it", repo=repo, agents=[bot(n) for n in names], start_plan=start_plan)
    out = asyncio.run(projects.create_project(body, background or BackgroundTasks(), user=owner, db=db))
    return out, {a.name: a for a in db.query(Agent).filter(Agent.project_id == out["id"]).all()}


def test_the_master_is_the_coding_bot_when_there_is_a_repository_and_the_project_says_so(db, owner):
    out, agents = make(db, owner)
    assert out["coding_bot_id"] == agents["Master"].id and out["repo"] == "acme/app"
    assert db.get(Project, out["id"]).coding_bot_id == agents["Master"].id  # saved, so pull requests reach it from the first one


def test_without_a_repository_there_is_nothing_for_a_coding_bot_to_do(db, owner):
    out, _ = make(db, owner, repo="")
    assert out["coding_bot_id"] == "" and db.get(Project, out["id"]).coding_bot_id == ""


def test_a_suggestion_named_master_is_not_made_and_a_repeated_name_gets_a_number(db, owner):
    out, agents = make(db, owner, names=("Master", "Ada"))
    assert set(agents) == {"Master", "Ada"} and out["coding_bot_id"] == agents["Master"].id  # "Master" is the orchestrator's name: that suggestion is not made
    out, agents = make(db, owner, names=("Ada", "Ada", "Grace"))
    assert set(agents) == {"Master", "Ada", "Ada 2", "Grace"}


def test_only_the_first_five_suggestions_become_bots(db, owner):
    out, agents = make(db, owner, names=("A1", "B2", "C3", "D4", "E5", "F6"))
    assert "F6" not in agents and len(agents) == 6  # the Master and five


def test_an_old_request_that_still_names_a_coding_bot_is_accepted_and_the_master_is_used(db, owner):
    body = projects.ProjectBody.model_validate({"name": "Proj", "repo": "acme/app", "coding_bot": "Ada", "start_plan": False, "agents": [bot("Ada").model_dump()]})
    out = asyncio.run(projects.create_project(body, BackgroundTasks(), user=owner, db=db))
    master = db.query(Agent).filter(Agent.project_id == out["id"], Agent.is_master == "true").one()
    assert out["coding_bot_id"] == master.id


def test_the_tweaks_setting_can_still_change_it_afterwards(db, owner):
    out, agents = make(db, owner)
    saved = projects.set_coding_bot(out["id"], projects.CodingBotBody(agent_id=agents["Linus"].id), user=owner, db=db)
    assert saved == {"coding_bot_id": agents["Linus"].id}


# ── a bot keeps the id its avatar was drawn from on the setup screen ─

def test_the_id_the_setup_screen_used_for_the_avatar_becomes_the_bots_id(db, owner):
    import uuid
    ids = [str(uuid.uuid4()) for _ in range(3)]
    body = projects.ProjectBody(name="Proj", goal="g", start_plan=False,
                                agents=[projects.SuggestedAgent(id=i, name=n, label="L", field="f", system_prompt="p") for i, n in zip(ids, ("Ada", "Grace", "Linus"))])
    out = asyncio.run(projects.create_project(body, BackgroundTasks(), user=owner, db=db))
    made = {a.name: a.id for a in db.query(Agent).filter(Agent.project_id == out["id"]).all()}
    assert [made["Ada"], made["Grace"], made["Linus"]] == ids


def test_an_id_that_is_not_a_fresh_uuid_is_ignored(db, owner):
    import uuid
    taken = str(uuid.uuid4())
    db.add(Project(id="proj_old", name="Old"))
    db.commit()
    db.add(Agent(id=taken, name="Existing", system_prompt="x", project_id="proj_old"))
    db.commit()
    dup = str(uuid.uuid4())
    used: set = set()
    assert projects._bot_id(db, dup, used) == dup and projects._bot_id(db, dup, used) != dup  # the same id twice in one request: only the first keeps it
    for bad in ("", "not-a-uuid", taken, str(uuid.uuid1()), "../../etc"):
        got = projects._bot_id(db, bad, set())
        assert got != bad and uuid.UUID(got).version == 4
