"""Run one flow on its own and see what it costs: the planning flow first. Owner only.

The instructions each flow gives its bots live in backend/flows/*.md and are read at every run, so a correction is an edit and a re-run."""
import json
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import func
from sqlalchemy.orm import Session

from core.access import get_current_user, get_project
from core.database import get_db
from core.models import Agent, DocumentRecord, Message, PriceItem, Project, Routine, Task, UsageEvent, User
from routes.wallet import _owner
from services import first_plan, payments

router = APIRouter()


def _brief(project: Project, user: User, db: Session, research: str, decisions: int | None) -> tuple[str, str, int]:
    """The planning brief this project would get, and the mode and number of questions it was written with."""
    team = [(a.name, a.label, a.field) for a in db.query(Agent).filter(Agent.project_id == project.id, Agent.is_master != "true").order_by(Agent.created_at)]
    mode, asks = first_plan.defaults()
    mode = research if research in first_plan.RESEARCH_MODES else mode
    asks = asks if decisions is None else max(0, min(5, decisions))
    try:
        text = first_plan.brief(project.name, project.goal or "", project.repo or "", project.summary or "", team, user.name, research=mode, decisions=asks,
                                docs=first_plan.research_docs(db, project.id))
    except (OSError, ValueError, KeyError, IndexError) as exc:  # a file being edited right now: say what is wrong with it
        raise HTTPException(status_code=422, detail=f"The planning instructions in backend/flows could not be read: {exc}")
    return text, mode, asks


@router.get("/api/flows/plan/preview")
def plan_preview(research: str = "", decisions: int = -1, project: Project = Depends(get_project), user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """The exact brief the Master would be given, without running anything: no model call, no tokens."""
    _owner(project, user, db)
    text, mode, asks = _brief(project, user, db, research, None if decisions < 0 else decisions)
    words = len(text.split())
    reusable = len(first_plan.research_docs(db, project.id))
    return {"text": text, "words": words, "tokens": round(words * 1.35), "research": mode, "decisions": asks, "reusable_docs": reusable}


class PlanRun(BaseModel):
    research: str = ""
    decisions: int | None = None


@router.post("/api/flows/plan/run", status_code=202)
async def plan_run(body: PlanRun, project: Project = Depends(get_project), user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """Run the planning flow again on this project, and only that: the Master is started on the brief, in the project's chat."""
    _owner(project, user, db)
    master = db.query(Agent).filter(Agent.project_id == project.id, Agent.is_master == "true").first()
    if master is None:
        raise HTTPException(status_code=404, detail="This project has no Master to plan with.")
    text, mode, asks = _brief(project, user, db, body.research, body.decisions)
    if not db.query(Task).filter(Task.agent_id == master.id, Task.title == first_plan.TITLE, Task.status == "working").first():
        db.add(Task(agent_id=master.id, title=first_plan.TITLE, status="working"))
        db.commit()
    since = datetime.utcnow()
    await first_plan.start(master.id, project.id, user.id, text)
    return {"started": True, "since": since.isoformat() + "Z", "research": mode, "decisions": asks}


@router.get("/api/flows/usage")
def usage(since: str, project: Project = Depends(get_project), user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """What the project's bots have spent since a moment: the meter to watch while a flow runs."""
    _owner(project, user, db)
    try:
        when = datetime.fromisoformat(since.replace("Z", "+00:00")).replace(tzinfo=None)
    except ValueError:
        raise HTTPException(status_code=422, detail="since must be an ISO time")
    rows = (db.query(UsageEvent.agent_id, func.sum(UsageEvent.input_tokens), func.sum(UsageEvent.output_tokens), func.sum(UsageEvent.cache_read_tokens), func.count())
            .filter(UsageEvent.project_id == project.id, UsageEvent.created_at >= when).group_by(UsageEvent.agent_id).all())
    names = {a.id: a.name for a in db.query(Agent).filter(Agent.project_id == project.id)}
    bots = sorted(({"name": names.get(aid, "?"), "input": int(i or 0), "output": int(o or 0), "calls": int(n)} for aid, i, o, _, n in rows), key=lambda b: -(b["input"] + b["output"]))
    return {"input": sum(b["input"] for b in bots), "output": sum(b["output"] for b in bots), "cache_read": sum(int(c or 0) for _, _, _, c, _ in rows), "calls": sum(b["calls"] for b in bots), "bots": bots}


# ── where the project is, and what each stage cost ───────────────────────

STEPS = (("plan", "Plan"), ("build", "Build"), ("live", "Live"), ("orders", "Orders"), ("payments", "Payments"))
RUNNING_FOR = timedelta(seconds=120)  # a bot that spent tokens this recently is still at work


def _done(db: Session, project: Project) -> dict[str, bool]:
    ids = [a.id for a in db.query(Agent).filter(Agent.project_id == project.id)]
    return {
        "plan": db.query(DocumentRecord).filter(DocumentRecord.project_id == project.id, DocumentRecord.title.ilike(f"{first_plan.DOC_PREFIX}%")).first() is not None,
        "build": payments.has_build(db, project.id),
        "live": bool(project.site_slug),
        "orders": bool(ids) and db.query(Routine).filter(Routine.agent_id.in_(ids), Routine.enabled.is_(True), Routine.schedule.like('%"on_order"%')).first() is not None,
        "payments": bool(project.payments_enabled) and db.query(PriceItem).filter(PriceItem.project_id == project.id).first() is not None,
    }


HINTS = {
    "plan": "The Master researches, asks you, and writes the plan",
    "build": "Approve the plan, then Muse builds it",
    "live": "Allow the publish card to put the site online",
    "orders": "Ask the Master to set up what each bot does on an order",
    "payments": "Turn on payments and set prices in the Wallet",
}


def phase_costs(db: Session, project: Project, user: User, now: datetime | None = None) -> list[dict]:
    """Each heading in this person's chats, with the time and bot tokens from when it began to when the next one began (or the last spending, for the newest)."""
    now = now or datetime.utcnow()
    ids = [a.id for a in db.query(Agent).filter(Agent.project_id == project.id)]
    if not ids:
        return []
    marks = (db.query(Message).filter(Message.agent_id.in_(ids), Message.user_id == user.id, Message.content.like('[[SOURCES:{"kind": "phase"%'))
             .order_by(Message.created_at, Message.id).all())
    out = []
    for i, row in enumerate(marks):
        try:
            card = json.loads(row.content[len("[[SOURCES:"):-2])
        except ValueError:
            continue
        start = row.created_at or now
        later = marks[i + 1].created_at if i + 1 < len(marks) else None
        q = db.query(func.coalesce(func.sum(UsageEvent.input_tokens + UsageEvent.output_tokens), 0), func.count(), func.max(UsageEvent.created_at)).filter(
            UsageEvent.project_id == project.id, UsageEvent.created_at >= start)
        if later is not None:
            q = q.filter(UsageEvent.created_at < later)
        tokens, calls, last = q.one()
        running = later is None and last is not None and now - last < RUNNING_FOR
        end = later or (now if running else (last or start))
        out.append({"id": row.id, "phase": card.get("phase", ""), "label": card.get("label", ""), "minutes": round(max(0.0, (end - start).total_seconds()) / 60, 1),
                    "tokens": int(tokens or 0), "calls": int(calls or 0), "running": bool(running)})
    return out


@router.get("/api/flows/path")
def path(project: Project = Depends(get_project), user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """The project's stages and which one it is at, with what the latest run of each cost. Owner only: the costs are the plan's."""
    _owner(project, user, db)
    done, costs = _done(db, project), phase_costs(db, project, user)
    latest = {c["phase"]: c for c in costs}
    current = next((sid for sid, _ in STEPS if not done[sid]), "")
    steps = [{"id": sid, "label": label, "state": "done" if done[sid] else "current" if sid == current else "pending", "hint": HINTS[sid], "cost": latest.get(sid)} for sid, label in STEPS]
    return {"steps": steps, "phases": costs}


@router.get("/api/builds/{row}/log")
def build_log_lines(row: int, after: int = 0, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """What Muse has printed for the build whose card is chat row `row`: the lines after the first `after`. Only the person whose chat it is."""
    from services import build_log

    msg = db.get(Message, row)
    if msg is None or msg.user_id != user.id or '"kind": "build"' not in msg.content:
        raise HTTPException(404, "No such build")
    lines, count = build_log.since(row, max(0, after))
    return {"lines": lines, "next": count, "running": '"status": "running"' in msg.content}


@router.get("/api/flows/intro")
async def flow_intro(project: Project = Depends(get_project), user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """The walkthrough for this project, written for it once and kept. Any member may read it; it costs one small model call, charged to whoever asks first."""
    from core.usage import set_usage_user
    from services import flow_intro as intro

    team = [(a.name, a.label or a.field or "") for a in db.query(Agent).filter(Agent.project_id == project.id, Agent.is_master != "true").order_by(Agent.created_at)]
    if project.flow_intro:
        try:
            return {**json.loads(project.flow_intro), "team": [n for n, _ in team[:3]]}
        except ValueError:
            pass
    set_usage_user(user.id)
    made = await intro.generate(project.name, project.goal or "", project.summary or "", team)
    if made != intro.fallback(project.name, project.goal or "", team):  # the plain version is not kept: the next ask tries the model again
        project.flow_intro = json.dumps(made)
        db.commit()
    return {**made, "team": [n for n, _ in team[:3]]}
