"""While the first build runs, the Master plans the next phase."""
import asyncio

from core.models import Agent, Project, Task
from services import next_phase


def test_brief_names_the_build_the_task_and_forbids_touching_it():
    t = next_phase.brief("Lumen", "Health app", "plan.md", [("Ada", "QA", "testing")])
    for fact in ("Lumen", "plan.md", "- Ada (QA, testing)", f'"{next_phase.TITLE}"', f'"{next_phase.DOC_PREFIX}: Lumen"', "request_decision", "Do not touch"):
        assert fact in t


def test_plans_once_per_project(db, monkeypatch):
    sent = []
    monkeypatch.setattr("routes.chat._start_kickoff", lambda *a, **k: sent.append(a))
    db.add(Project(id="p1", name="P"))
    db.commit()
    db.add(Agent(id="m1", name="M", project_id="p1", is_master="true", system_prompt="x"))
    db.commit()
    for _ in range(2):
        asyncio.run(next_phase.begin("p1", "u", "m1", "P", "", "plan.md"))
    assert len(sent) == 1 and sent[0][0] == "m1"
    assert db.query(Task).filter(Task.agent_id == "m1", Task.title == next_phase.TITLE).count() == 1
