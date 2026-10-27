"""Router for document CRUD operations with access control and sharing."""
from datetime import datetime, timezone
import json
from typing import Optional
import uuid

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy.orm import Session

from core.access import COOKIE
from core.database import get_db
from core.models import AuthSession, Doc as DocModel, User
from core.usage import current_user


class DocCreate(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    content: str = Field(min_length=1)
    tags: Optional[list[str]] = Field(default_factory=list)
    shared_with: Optional[list[str]] = Field(default_factory=list)

    @field_validator("title")
    @classmethod
    def validate_title(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("title must not be empty or whitespace only")
        if len(v) > 200:
            raise ValueError("title must not exceed 200 characters")
        return v

    @field_validator("content")
    @classmethod
    def validate_content(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("content must not be empty or whitespace only")
        return v

    @field_validator("tags", "shared_with", mode="before")
    @classmethod
    def validate_list_fields(cls, v):
        if v is None:
            return []
        if not isinstance(v, list):
            raise ValueError("must be a list of strings")
        return v


class DocShare(BaseModel):
    users: Optional[list[str]] = None
    shared_with: Optional[list[str]] = None

    @field_validator("users", "shared_with", mode="before")
    @classmethod
    def validate_list(cls, v):
        if v is None:
            return None
        if not isinstance(v, list):
            raise ValueError("must be a list of user IDs or emails")
        return v


class Doc(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    title: str
    content: str
    tags: list[str] = Field(default_factory=list)
    created_by: Optional[str] = ""
    user_id: Optional[str] = ""
    shared_with: list[str] = Field(default_factory=list)
    created_at: datetime
    updated_at: datetime

    @field_validator("tags", "shared_with", mode="before")
    @classmethod
    def coerce_json_lists(cls, v):
        if v is None:
            return []
        if isinstance(v, str):
            try:
                parsed = json.loads(v)
                if isinstance(parsed, list):
                    return parsed
            except Exception:
                return [t.strip() for t in v.split(",") if t.strip()]
        return v

    @field_validator("created_at", "updated_at", mode="after")
    @classmethod
    def ensure_utc(cls, v: datetime) -> datetime:
        if v.tzinfo is None:
            return v.replace(tzinfo=timezone.utc)
        return v.astimezone(timezone.utc)


def get_caller_user_identities(request: Request, db: Session) -> tuple[Optional[str], set[str]]:
    """Resolve caller user_id and set of identifiers (id, email) if authenticated."""
    token = request.cookies.get(COOKIE, "")
    if token:
        row = db.get(AuthSession, token)
        if row:
            now = datetime.now(timezone.utc)
            expires = row.expires_at
            if expires is not None and expires.tzinfo is None:
                expires = expires.replace(tzinfo=timezone.utc)
            if expires and expires >= now:
                user = db.get(User, row.user_id)
                if user:
                    ident = {user.id}
                    if user.email:
                        ident.add(user.email.lower())
                    return user.id, ident

    x_user = request.headers.get("X-User-Id") or request.headers.get("x-user-id")
    if x_user:
        u_str = x_user.strip()
        db_user = db.get(User, u_str) or db.query(User).filter(User.email == u_str).first()
        if db_user:
            ident = {db_user.id}
            if db_user.email:
                ident.add(db_user.email.lower())
            return db_user.id, ident
        return u_str, {u_str, u_str.lower()}

    ctx_user = current_user()
    if ctx_user:
        u_str = ctx_user.strip()
        db_user = db.get(User, u_str)
        if db_user:
            ident = {db_user.id}
            if db_user.email:
                ident.add(db_user.email.lower())
            return db_user.id, ident
        return u_str, {u_str, u_str.lower()}

    return None, set()


def user_can_see_doc(doc: DocModel, user_id: Optional[str], identifiers: set[str]) -> bool:
    """Return True if the user created the document or if it was shared with them."""
    if user_id is None:
        # Unauthenticated / guest: can only see unowned documents
        return not doc.user_id and not doc.created_by

    # Creator check
    if (doc.user_id and doc.user_id in identifiers) or (doc.created_by and doc.created_by in identifiers):
        return True

    # Shared with check
    shared = doc.shared_with or []
    if isinstance(shared, str):
        try:
            shared = json.loads(shared)
        except Exception:
            shared = [s.strip() for s in shared.split(",") if s.strip()]
    shared_lower = {str(s).lower() for s in shared}
    if any(i.lower() in shared_lower for i in identifiers):
        return True

    return False


def sync_document_records_to_docs(db: Session):
    """Ensure bot-generated files (DocumentRecord) are unified into DocModel."""
    from core.models import DocumentRecord
    from tools.documents import document_path

    try:
        recs = db.query(DocumentRecord).all()
        for rec in recs:
            existing = db.query(DocModel).filter((DocModel.id == rec.filename) | (DocModel.title == rec.title)).first()
            shared_list = rec.shared_with or []
            if isinstance(shared_list, str):
                try:
                    shared_list = json.loads(shared_list)
                except Exception:
                    shared_list = []

            path = document_path(rec.filename)
            doc_content = ""
            if path and path.is_file():
                try:
                    doc_content = path.read_text(encoding="utf-8", errors="replace")[:20000]
                except Exception:
                    doc_content = f"Document {rec.filename}"
            else:
                doc_content = f"Document {rec.filename}"

            fmt = path.suffix.lstrip(".").lower() if path else "md"
            tags = ["bot", fmt] if fmt else ["bot"]

            if not existing:
                doc_time = rec.created_at or datetime.now(timezone.utc)
                if doc_time.tzinfo is None:
                    doc_time = doc_time.replace(tzinfo=timezone.utc)
                db.add(DocModel(
                    id=rec.filename,
                    title=rec.title or rec.filename,
                    content=doc_content,
                    tags=tags,
                    created_by=rec.created_by or rec.user_id or "",
                    user_id=rec.user_id or rec.created_by or "",
                    shared_with=shared_list,
                    created_at=doc_time,
                    updated_at=doc_time,
                ))
            else:
                dirty = False
                if not existing.created_by and (rec.created_by or rec.user_id):
                    existing.created_by = rec.created_by or rec.user_id or ""
                    existing.user_id = rec.user_id or rec.created_by or ""
                    dirty = True
                existing_shared = list(existing.shared_with or [])
                if isinstance(existing_shared, str):
                    try:
                        existing_shared = json.loads(existing_shared)
                    except Exception:
                        existing_shared = []
                existing_lower = {str(s).lower() for s in existing_shared}
                for s in shared_list:
                    if str(s).lower() not in existing_lower:
                        existing_shared.append(s)
                        dirty = True
                if dirty:
                    existing.shared_with = existing_shared
        db.commit()
    except Exception:
        db.rollback()


router = APIRouter()


@router.post("/docs", response_model=Doc, status_code=status.HTTP_201_CREATED)
def create_doc(body: DocCreate, request: Request, db: Session = Depends(get_db)):
    user_id, _ = get_caller_user_identities(request, db)
    now = datetime.now(timezone.utc)
    doc = DocModel(
        id=str(uuid.uuid4()),
        title=body.title,
        content=body.content,
        tags=body.tags if body.tags is not None else [],
        created_by=user_id or "",
        user_id=user_id or "",
        shared_with=body.shared_with if body.shared_with is not None else [],
        created_at=now,
        updated_at=now,
    )
    db.add(doc)
    db.commit()
    db.refresh(doc)
    return doc


@router.get("/docs", response_model=list[Doc])
def list_docs(request: Request, db: Session = Depends(get_db)):
    sync_document_records_to_docs(db)
    user_id, identifiers = get_caller_user_identities(request, db)
    all_docs = db.query(DocModel).order_by(DocModel.created_at.desc(), DocModel.id.desc()).all()
    return [d for d in all_docs if user_can_see_doc(d, user_id, identifiers)]


@router.get("/docs/{id}", response_model=Doc)
def get_doc(id: str, request: Request, db: Session = Depends(get_db)):
    sync_document_records_to_docs(db)
    doc = db.query(DocModel).filter(DocModel.id == id).first()
    if not doc:
        from core.models import DocumentRecord
        rec = db.get(DocumentRecord, id)
        if rec:
            doc = db.query(DocModel).filter((DocModel.id == rec.filename) | (DocModel.title == rec.title)).first()
        if not doc:
            doc = db.query(DocModel).filter(DocModel.title == id).first()
    if not doc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Doc not found")
    user_id, identifiers = get_caller_user_identities(request, db)
    if not user_can_see_doc(doc, user_id, identifiers):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Doc not found")
    return doc


@router.post("/docs/{id}/share", response_model=Doc)
def share_doc(id: str, body: DocShare, request: Request, db: Session = Depends(get_db)):
    doc = db.query(DocModel).filter(DocModel.id == id).first()
    if not doc:
        from core.models import DocumentRecord
        rec = db.get(DocumentRecord, id)
        if rec:
            doc = db.query(DocModel).filter((DocModel.id == rec.filename) | (DocModel.title == rec.title)).first()
        if not doc:
            doc = db.query(DocModel).filter(DocModel.title == id).first()
    if not doc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Doc not found")
    user_id, identifiers = get_caller_user_identities(request, db)
    if not user_can_see_doc(doc, user_id, identifiers):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Doc not found")

    if doc.created_by and user_id and doc.created_by not in identifiers and doc.user_id not in identifiers:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Only the document creator can share it")

    new_users = (body.users or []) + (body.shared_with or [])
    current = list(doc.shared_with or [])
    for u in new_users:
        if u and u not in current:
            current.append(u)
    doc.shared_with = current
    doc.updated_at = datetime.now(timezone.utc)

    from core.models import DocumentRecord
    doc_rec = db.get(DocumentRecord, id) or db.query(DocumentRecord).filter((DocumentRecord.filename == id) | (DocumentRecord.title == doc.title)).first()
    if doc_rec:
        rec_shared = list(doc_rec.shared_with or [])
        if isinstance(rec_shared, str):
            try: rec_shared = json.loads(rec_shared)
            except Exception: rec_shared = []
        for u in new_users:
            if u and u not in rec_shared and u.lower() not in [str(s).lower() for s in rec_shared]:
                rec_shared.append(u)
        doc_rec.shared_with = rec_shared

    db.commit()
    db.refresh(doc)
    return doc

@router.delete("/docs/{id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_doc(id: str, request: Request, db: Session = Depends(get_db)):
    from tools.documents import delete_document
    from core.models import DocumentRecord

    user_id, identifiers = get_caller_user_identities(request, db)
    doc = db.get(DocModel, id)
    if not doc:
        rec = db.get(DocumentRecord, id)
        if rec:
            doc = db.query(DocModel).filter((DocModel.id == rec.filename) | (DocModel.title == rec.title)).first()
        if not doc:
            doc = db.query(DocModel).filter(DocModel.title == id).first()
    if not doc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Doc not found")

    if not user_can_see_doc(doc, user_id, identifiers):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Doc not found")

    target_filename = doc.id
    rec = db.get(DocumentRecord, doc.id) or db.query(DocumentRecord).filter((DocumentRecord.filename == doc.id) | (DocumentRecord.title == doc.title)).first()
    if rec:
        target_filename = rec.filename
        db.delete(rec)
    db.delete(doc)
    db.commit()

    try:
        delete_document(target_filename)
    except Exception:
        pass
    return None

