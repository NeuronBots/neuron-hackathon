"""While the first build runs, the team plans the next phase with the person.

The build takes a while and the person would only wait for it. So the moment it starts, the Master is started on a second board task: work out what the
next phase of the project should be, take the few decisions that matter through Decisions pending, and write ONE plan document for it. Like the first plan
this is a document, not code: nothing is built from it until the person has read it, and the build in progress is never touched."""
from __future__ import annotations

import asyncio
import logging

from core.database import SessionLocal
from core.models import Agent, Task

log = logging.getLogger(__name__)

DOC_PREFIX = "Next phase plan"  # not the first plan's prefix: that one gets the build button
TITLE = "Plan the next phase"  # the board task's title; decisions about this plan use it too, so they stay attached to it


def brief(name: str, goal: str, plan_doc: str, team: list[tuple[str, str, str]]) -> str:
    """What the Master is told when the first build starts."""
    mates = "\n".join(f"- {n}" + (f" ({', '.join(x for x in (label, field) if x)})" if label or field else "") for n, label, field in team) or "- (no teammates yet: do their parts yourself)"
    return (
        f"Muse has just started building the first version of {name} in a sandbox from the approved first-build plan ({plan_doc}). It takes a while. Meanwhile, now and on your own, "
        "plan the NEXT PHASE with the person: what to build after the first version, and why.\n\n"
        f"## The project\nName: {name}\nGoal: {goal or '(not given)'}\n\n## Your team\n{mates}\n\n"
        "## How to work\n"
        f"1. Put the work on the board first: add_board_task with the title \"{TITLE}\" (it may already be there; then use it). Use that exact title whenever you call "
        "request_decision or complete_board_task for this plan.\n"
        "2. Read the first-build plan with read_document, and what the team saved for it. The first version is not finished, so plan from what it was meant to deliver, "
        "and say so in the plan where the next phase depends on how the build turns out. You will be told when the build ends.\n"
        "3. Talk it through with the person. Open with one short message saying what you are doing, then take the decisions that decide the next phase with request_decision: "
        f"title \"{TITLE}\", one clear question, your recommendation, and two to four options (best first). Typical ones: what matters most after the first version "
        "(depth, new features, polish, scale), which of the first plan's left-out items come in, who it is for next, and any deadline. Fold related questions into one decision, "
        "put the most important first, ask at most four in all over at most two rounds, and take each answer as final. Ask teammates with ask_teammate for their field's view where it changes the choice.\n"
        f"4. Write the plan: call create_document ONCE, as md, titled \"{DOC_PREFIX}: {name}\". Goal and what \"done\" means for the phase, what is in and out, the decisions made and why, "
        "the ordered steps with what to build and how to tell each is finished, risks, and assumptions. Write the plan itself, not a description of one.\n"
        f"5. Finish with complete_board_task for \"{TITLE}\", give the person the document link exactly as the tool returns it, say in three lines what is in it, and ask what to change.\n\n"
        "## Rules\n"
        "- Do not touch, restart or change the running build, and do not start another one. Do not write or push code, and do not create Jira issues or anything outside the app: this is a plan.\n"
        "- When the build's result arrives, handle it first (the person is waiting for it), then come back to this plan.\n"
    )


def _queue(project_id: str, agent_id: str) -> tuple[str, list[tuple[str, str, str]]] | None:
    """The Master to plan, its team, and the task on its board. None when the project has no Master or this plan is already on its board."""
    db = SessionLocal()
    try:
        master = db.query(Agent).filter(Agent.project_id == project_id, Agent.is_master == "true").first() or db.get(Agent, agent_id)
        if master is None or db.query(Task).filter(Task.agent_id == master.id, Task.title == TITLE).first():
            return None
        db.add(Task(agent_id=master.id, title=TITLE, status="working"))
        team = [(a.name, a.label, a.field) for a in db.query(Agent).filter(Agent.project_id == project_id, Agent.id != master.id).all()]
        db.commit()
        return master.id, team
    finally:
        db.close()


async def begin(project_id: str, user_id: str, agent_id: str, name: str, goal: str, plan_doc: str) -> None:
    """Start the Master on the next phase. Never raises: the build matters more than this."""
    from routes.chat import _start_kickoff

    try:
        queued = await asyncio.to_thread(_queue, project_id, agent_id)
        if queued is None:
            return
        master_id, team = queued
        _start_kickoff(master_id, "Neuron", project_id, brief(name, goal, plan_doc, team), user_id)
    except Exception:
        log.exception("Could not start planning the next phase of %s", name)
