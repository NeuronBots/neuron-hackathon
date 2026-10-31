"""Rows a background job writes into a person's chat with a bot: a pill that shows progress and then how it ended, and plain rows (links) after it."""
from __future__ import annotations

import json


def card(kind: str, **fields) -> str:
    """A pill, in the form the chat draws (`[[SOURCES:{...}]]`). The brackets inside it are broken so a value cannot end it early."""
    return "[[SOURCES:" + json.dumps({"kind": kind, **fields}).replace("]]", "] ]").replace("[[", "[ [") + "]]"


def write_row(agent_id: str, user_id: str, text: str, row_id: int | None = None) -> int:
    """Add a row to this person's chat with the bot, or overwrite row_id in place. Returns the row's id."""
    from core.database import SessionLocal
    from core.models import Message

    db = SessionLocal()
    try:
        row = db.get(Message, row_id) if row_id else None
        if row is None:
            row = Message(agent_id=agent_id, user_id=user_id, role="assistant", content=text)
            db.add(row)
        row.content = text
        db.commit()
        return row.id
    finally:
        db.close()
