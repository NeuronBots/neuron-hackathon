"""A stripped-down git for a bot's memory: the prod version is what the bot reads, a draft waits for a person, older versions can be rolled back to.

core_memory changes the bot makes itself are staged as drafts; memory notes and anything a person did are promoted at once."""
from datetime import datetime, timezone

from sqlalchemy.orm import Session, object_session

from core.models import Agent, MemoryVersion

KINDS = ("core_memory", "memory")
KEEP = 20  # versions kept per bot and kind, newest first; the prod row and a draft are never dropped


def _rows(db: Session, agent_id: str, kind: str):
    return db.query(MemoryVersion).filter(MemoryVersion.agent_id == agent_id, MemoryVersion.kind == kind)


def draft_of(agent: Agent, kind: str) -> MemoryVersion | None:
    return _rows(object_session(agent), agent.id, kind).filter(MemoryVersion.is_draft).first()


def _baseline(agent: Agent, kind: str, db: Session) -> None:
    """The text the bot has now, saved as prod the first time, so there is something to roll back to."""
    current = (getattr(agent, kind) or "").strip()
    if current and not _rows(db, agent.id, kind).filter(MemoryVersion.is_prod).first():
        db.add(MemoryVersion(agent_id=agent.id, kind=kind, content=current, is_prod=True, source="before versions"))
        db.flush()


def set_prod(agent: Agent, kind: str, text: str, source: str) -> None:
    """Make `text` the live version at once: the old prod stays as history."""
    db = object_session(agent)
    _baseline(agent, kind, db)
    _rows(db, agent.id, kind).filter(MemoryVersion.is_prod).update({"is_prod": False})
    db.add(MemoryVersion(agent_id=agent.id, kind=kind, content=text, is_prod=True, source=source))
    setattr(agent, kind, text)
    db.flush()
    old = [r.id for r in _rows(db, agent.id, kind).filter(~MemoryVersion.is_prod, ~MemoryVersion.is_draft).order_by(MemoryVersion.id.desc()).offset(KEEP)]
    if old:
        db.query(MemoryVersion).filter(MemoryVersion.id.in_(old)).delete(synchronize_session=False)


def stage(agent: Agent, kind: str, text: str, source: str) -> None:
    """Hold `text` as the pending change; a second change before anyone looks replaces the first (it was built on top of it)."""
    db = object_session(agent)
    _baseline(agent, kind, db)
    draft = draft_of(agent, kind)
    if draft is None:
        db.add(MemoryVersion(agent_id=agent.id, kind=kind, content=text, is_draft=True, source=source))
    else:
        draft.content, draft.source, draft.created_at = text, source, datetime.now(timezone.utc)
    db.flush()


def promote(agent: Agent, version: MemoryVersion) -> None:
    """Approve a draft, or roll back to an older version: it becomes prod and the bot's prompt shows it from the next message."""
    db = object_session(agent)
    _rows(db, agent.id, version.kind).filter(MemoryVersion.is_prod).update({"is_prod": False})
    version.is_prod, version.is_draft = True, False
    setattr(agent, version.kind, version.content)
    db.flush()


def reject(version: MemoryVersion) -> None:
    object_session(version).delete(version)
