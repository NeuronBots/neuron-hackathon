"""A project's stored data (orders, bookings...) for the Data view. The bots write it through tools/records.py."""
from fastapi import APIRouter, Depends
from sqlalchemy import func
from sqlalchemy.orm import Session

from core.access import get_project
from core.database import get_db
from core.models import Project, ProjectRecord

router = APIRouter()


@router.get("/api/records")
def list_records(collection: str = "", project: Project = Depends(get_project), db: Session = Depends(get_db)):
    """Every collection with its size, and the newest records of one (the first when none is named)."""
    mine = db.query(ProjectRecord).filter(ProjectRecord.project_id == project.id)
    counts = mine.with_entities(ProjectRecord.collection, func.count()).group_by(ProjectRecord.collection).order_by(ProjectRecord.collection).all()
    names = [c for c, _ in counts]
    chosen = collection if collection in names else (names[0] if names else "")
    rows = mine.filter(ProjectRecord.collection == chosen).order_by(ProjectRecord.created_at.desc()).limit(500).all() if chosen else []
    return {
        "collections": [{"name": c, "count": n} for c, n in counts],
        "collection": chosen,
        "records": [{"id": r.id, "status": r.status, "data": r.data, "created_at": r.created_at.isoformat() + "Z", "updated_at": r.updated_at.isoformat() + "Z"} for r in rows],
    }
