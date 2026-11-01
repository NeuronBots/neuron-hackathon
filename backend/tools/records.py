"""A project's own data (orders, bookings, stock...), kept in the app's database. No fixed schema: a record is JSON in a named collection."""
from __future__ import annotations

import json
import re

from sqlalchemy.orm import Session

from core.models import ProjectRecord

NAMES = ("save_record", "list_records")
MAX_BYTES = 20_000  # one record
MAX_RECORDS = 5000  # one collection

TOOL_DEFS = [
    {
        "name": "save_record",
        "description": (
            "Store a piece of the project's data (an order, a booking, a stock level) in a named collection such as orders. Pass id (from list_records) to change a record: "
            "the fields you send are merged into it. Never overwrite what the customer filled in. Keep to the shape the collection's other records already have. It stays in Neuron's database and sends nothing outside."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "collection": {"type": "string", "description": "Lowercase name, e.g. orders"},
                "data": {"type": "object", "description": "The fields to store or change"},
                "id": {"type": "string", "description": "A record's id, to change it instead of adding one"},
                "status": {"type": "string", "description": "Where it stands, e.g. new, confirmed, ready, done"},
                "note": {"type": "string", "description": "What you found or did, kept under your name next to teammates' notes. Put it here, not in `data`: other bots write at the same time, and the customer's own fields must stay as they were."},
            },
            "required": ["collection"],
        },
    },
    {
        "name": "list_records",
        "description": "Read the project's stored data, newest first, each with its id and status. Without a collection it lists the collections and how many records each has.",
        "input_schema": {
            "type": "object",
            "properties": {
                "collection": {"type": "string"},
                "status": {"type": "string", "description": "Only records with this status"},
                "limit": {"type": "integer", "description": "Default 20, at most 100"},
            },
        },
    },
]


def add(project_id: str, collection: str, data: dict, status: str, db: Session) -> ProjectRecord:
    """Store one record. Raises ValueError with what the bot should hear."""
    collection = collection.strip().lower()
    if not re.fullmatch(r"[a-z0-9_-]{1,60}", collection):
        raise ValueError("a collection name is lowercase letters, digits, - or _, up to 60 characters")
    if len(json.dumps(data)) > MAX_BYTES:
        raise ValueError(f"a record is at most {MAX_BYTES} characters")
    if db.query(ProjectRecord).filter(ProjectRecord.project_id == project_id, ProjectRecord.collection == collection).count() >= MAX_RECORDS:
        raise ValueError(f"{collection} is full ({MAX_RECORDS} records)")
    row = ProjectRecord(project_id=project_id, collection=collection, data=data, status=status.strip()[:40])
    db.add(row)
    db.commit()
    return row


def _show(r: ProjectRecord) -> str:
    return f"{r.id} | {r.status or '-'} | {r.created_at:%Y-%m-%d %H:%M}Z | {json.dumps(r.data, ensure_ascii=False)}"


def with_note(data: dict, who: str, note: str) -> dict:
    """The record's data with `note` added under the writer's name. A new dict, so teammates' notes and the customer's fields are left alone."""
    return {**data, "bot_notes": {**(data.get("bot_notes") if isinstance(data.get("bot_notes"), dict) else {}), who: note.strip()[:2000]}}


def run(name: str, args: dict, project_id: str, who: str, db: Session) -> str:
    from services.review_render import untrusted_block

    try:
        if name == "save_record":
            data = args.get("data") if isinstance(args.get("data"), dict) else {}
            note = str(args.get("note") or "")
            if rid := str(args.get("id") or ""):
                row = db.query(ProjectRecord).filter(ProjectRecord.id == rid).with_for_update().first()  # one writer at a time: several bots answer the same order together
                if not row or row.project_id != project_id:
                    return "Error: no such record. Use an id from list_records."
                merged = {**row.data, **data}
                merged = with_note(merged, who, note) if note.strip() else merged
                if len(json.dumps(merged)) > MAX_BYTES:
                    return f"Error: a record is at most {MAX_BYTES} characters."
                row.data = merged  # a new dict: SQLAlchemy does not see changes made inside a JSON column
                if args.get("status"):
                    row.status = str(args["status"]).strip()[:40]
                db.commit()
                return "Saved: " + _show(row)
            return "Added: " + _show(add(project_id, str(args.get("collection") or ""), with_note(data, who, note) if note.strip() else data, str(args.get("status") or ""), db))
        collection = str(args.get("collection") or "").strip().lower()
        q = db.query(ProjectRecord).filter(ProjectRecord.project_id == project_id)
        if not collection:
            from sqlalchemy import func
            counts = q.with_entities(ProjectRecord.collection, func.count()).group_by(ProjectRecord.collection).all()
            return "\n".join(f"{c}: {n} records" for c, n in counts) or "No data stored yet."
        q = q.filter(ProjectRecord.collection == collection)
        if args.get("status"):
            q = q.filter(ProjectRecord.status == str(args["status"]).strip())
        rows = q.order_by(ProjectRecord.created_at.desc()).limit(min(int(args.get("limit") or 20), 100)).all()
        # visitors' words end up in records, so they are read as data, never as instructions
        return untrusted_block(f"Stored {collection}", "\n".join(_show(r) for r in rows)) if rows else f"No {collection} records."
    except (ValueError, TypeError) as exc:
        return f"Error: {exc}"
