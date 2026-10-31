"""Neuron hosts a project's built site: the static files of its build archive are served publicly at /sites/<slug>/, and a visitor's message
from the page reaches the Master. Hosting is for static sites (index.html at the archive's root); the pages are served sandboxed (routes/sites.py),
so they can never read the app's login."""
from __future__ import annotations

import asyncio
import io
import os
import re
import shutil
import zipfile
from pathlib import Path

from core.models import Agent, DocumentRecord, Project, ProjectMember
from tools.documents import DOC_DIR, document_path

SITES = Path(os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "data", "sites")))
MAX_FILE, MAX_TOTAL, MAX_FILES = 5_000_000, 50_000_000, 500
SKIP = {".git", "node_modules", ".neuron"}  # never part of a site
EDITABLE = {".html", ".css", ".js", ".json", ".svg", ".txt", ".md", ".webmanifest"}  # what a bot may rewrite: text, never binaries
ACTION_MAX = 4000  # characters of one visitor message


def base_url() -> str:
    """The address people outside reach this server at. Set SITES_PUBLIC_URL in production."""
    return os.getenv("SITES_PUBLIC_URL", "http://localhost:8000").rstrip("/")


def url(slug: str) -> str:
    return f"{base_url()}/sites/{slug}/"


def slug_for(project: Project) -> str:
    return (re.sub(r"[^a-z0-9]+", "-", project.name.lower()).strip("-")[:40] or "site") + "-" + project.id[:6]


def resolve(slug: str, rel: str) -> Path | None:
    """The file a visitor asked for inside one site, or None. A folder answers with its index.html; nothing outside the site is reachable."""
    root = (SITES / slug).resolve()
    if not re.fullmatch(r"[a-z0-9-]{1,60}", slug or "") or not root.is_dir():
        return None
    target = (root / (rel or "")).resolve()
    if target.is_dir():
        target = target / "index.html"
    return target if target.is_relative_to(root) and target.is_file() else None


def _unpack(data: bytes, dest: Path) -> int:
    """The archive's files into dest. Raises ValueError for anything that is not a plain static site."""
    count = total = 0
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        for info in z.infolist():
            name = info.filename.replace("\\", "/")
            parts = [p for p in name.split("/") if p]
            if info.is_dir() or not parts or name.startswith("/") or ".." in parts or SKIP & set(parts):
                continue
            count += 1
            total += info.file_size
            if info.file_size > MAX_FILE or total > MAX_TOTAL or count > MAX_FILES:
                raise ValueError("The site is too big to host here (at most 500 files, 5 MB each, 50 MB in all).")
            out = dest.joinpath(*parts)
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_bytes(z.read(info))
    if not (dest / "index.html").is_file():
        raise ValueError("The build has no index.html at its root, so it is not a static site Neuron can host.")
    return count


def publish(project_id: str, archive: str) -> str:
    """Host this build archive (a document of the project) as the project's site, replacing the one before. Returns the public address."""
    from core.database import SessionLocal

    path = document_path(archive)
    if path is None or path.suffix != ".zip" or not path.is_file():
        raise ValueError("That archive could not be found.")
    db = SessionLocal()
    try:
        project = db.get(Project, project_id)
        rec = db.get(DocumentRecord, archive)
        if project is None or rec is None or rec.project_id != project_id:
            raise ValueError("That archive is not on this project.")
        slug = project.site_slug or slug_for(project)
        SITES.mkdir(parents=True, exist_ok=True)
        fresh = SITES / f".{slug}.new"
        shutil.rmtree(fresh, ignore_errors=True)
        fresh.mkdir()
        try:
            _unpack(path.read_bytes(), fresh)
        except (ValueError, zipfile.BadZipFile) as exc:
            shutil.rmtree(fresh, ignore_errors=True)
            raise ValueError(str(exc) if isinstance(exc, ValueError) else "That archive is not a zip.")
        live = SITES / slug
        shutil.rmtree(live, ignore_errors=True)
        fresh.rename(live)
        project.site_slug = slug
        db.commit()
        return url(slug)
    finally:
        db.close()


def write_file(project_id: str, rel: str, content: str) -> str:
    """Change one text file of the hosted site. Returns the public address."""
    from core.database import SessionLocal

    db = SessionLocal()
    try:
        project = db.get(Project, project_id)
        slug = project.site_slug if project else None
    finally:
        db.close()
    if not slug:
        raise ValueError("This project's site is not published yet.")
    parts = [p for p in rel.replace("\\", "/").split("/") if p]
    if not parts or ".." in parts or SKIP & set(parts) or Path(parts[-1]).suffix.lower() not in EDITABLE or len(content) > MAX_FILE:
        raise ValueError("That path is not a text file this tool may change (html, css, js, json, svg, txt, md).")
    target = (SITES / slug).joinpath(*parts)
    if not target.resolve().is_relative_to((SITES / slug).resolve()):
        raise ValueError("That path is outside the site.")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    return url(slug)


def latest_archive(project_id: str) -> str:
    from core.database import SessionLocal

    db = SessionLocal()
    try:
        row = (db.query(DocumentRecord).filter(DocumentRecord.project_id == project_id, DocumentRecord.filename.like("%-first-build-%.zip"))
               .order_by(DocumentRecord.created_at.desc()).first())
        return row.filename if row else ""
    finally:
        db.close()


def run(name: str, args: dict, agent_id: str) -> str:
    """publish_site and update_site, once a person has allowed them. Blocking."""
    from core.database import SessionLocal

    db = SessionLocal()
    try:
        agent = db.get(Agent, agent_id)
        project_id = agent.project_id if agent else ""
    finally:
        db.close()
    try:
        if name == "publish_site":
            archive = str(args.get("archive") or "") or latest_archive(project_id)
            if not archive:
                return "Error: this project has no saved build to publish yet."
            return f"The site is live and public at {publish(project_id, archive)}. Give people exactly this address."
        address = write_file(project_id, str(args.get("path") or ""), str(args.get("content") or ""))
        return f"Changed {args.get('path')}. It is live now at {address}"
    except ValueError as exc:
        return f"Error: {exc}"


def _run_order_routines(project_id: str, message: str) -> int:
    """Start every enabled order routine of this project's bots on the visitor's message. Returns how many started."""
    from core.database import SessionLocal
    from core.models import Routine
    from services import routines

    db = SessionLocal()
    try:
        ids = [r.id for r in db.query(Routine).join(Agent, Agent.id == Routine.agent_id)
               .filter(Agent.project_id == project_id, Routine.enabled.is_(True), Routine.schedule.like('%"on_order"%'))]
    finally:
        db.close()
    return sum(routines.fire(i, message[:ACTION_MAX]) for i in ids)


async def deliver(project: Project, message: str) -> None:
    """A visitor's message from the page. With order routines (allowed by the owner) each one runs on it; without, the Master is told, as data and with no tools."""
    from core.database import SessionLocal
    from routes.chat import _start_kickoff
    from services.review_render import untrusted_block

    def who() -> tuple[str, str]:
        db = SessionLocal()
        try:
            master = db.query(Agent).filter(Agent.project_id == project.id, Agent.is_master == "true").first()
            owner = db.query(ProjectMember.user_id).filter(ProjectMember.project_id == project.id, ProjectMember.role == "owner").first()
            return (master.id if master else ""), (owner[0] if owner else "")
        finally:
            db.close()

    master_id, owner_id = await asyncio.to_thread(who)
    if not master_id:
        return
    if await asyncio.to_thread(_run_order_routines, project.id, message):
        return  # the owner allowed these routines: they are what happens when an order comes in
    text = (f"A visitor of the live website of {project.name} just sent this from the page (an order, a question or a form). Tell the person what came in, in one or two lines, "
            "and what you suggest doing about it. You cannot act on it yourself: they decide.\n\n" + untrusted_block("Visitor message", message[:ACTION_MAX]))
    _start_kickoff(master_id, "Website", project.id, text, owner_id, no_tools=True)
