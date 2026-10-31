"""A bot's goal is built from the project it is part of and what the team has produced for it."""
from datetime import datetime

from core.models import Agent, DocumentRecord, Project
from services import goals


def test_specialist_record_carries_the_project_and_its_documents(db):
    db.add(Project(id="p1", name="Lumen", goal="Health app for runners", summary="React app"))
    db.commit()
    db.add_all([Agent(id="m", name="Ada", project_id="p1", is_master="true", system_prompt="x"),
                Agent(id="q", name="Qin", project_id="p1", field="QA", system_prompt="Tests things")])
    db.add(DocumentRecord(filename="plan.md", project_id="p1", title="First build plan: Lumen", created_at=datetime(2026, 1, 1)))
    db.commit()
    record = goals._record(db.get(Agent, "q"), "u1", db)
    assert "Health app for runners" in record and "First build plan: Lumen" in record and "Teammate: Qin" in record
