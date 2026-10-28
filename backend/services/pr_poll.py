"""A safety net for missed webhooks: GitHub gives up on a delivery after 10 seconds and never retries it, and this server can be
unreachable for a moment. Every few minutes, open PRs on linked repos are listed and any that have no review yet are handed to the
same flow the webhook uses."""
from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timedelta, timezone

import httpx
from sqlalchemy import func

from core.database import SessionLocal
from core.models import Project, ProjectMember
from services import code_review

log = logging.getLogger(__name__)

INTERVAL = int(os.getenv("REVIEW_POLL_SECONDS", "300"))  # 0 turns polling off
MAX_AGE = timedelta(hours=int(os.getenv("REVIEW_POLL_MAX_AGE_HOURS", "24")))  # an old PR nobody has touched is not reviewed out of the blue


async def open_prs(repo: str, token: str) -> list[dict]:
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.get(f"https://api.github.com/repos/{repo}/pulls", params={"state": "open", "sort": "updated", "direction": "desc", "per_page": 50},
                             headers={"Authorization": "Bearer " + token, "Accept": "application/vnd.github+json"})
    if r.status_code != 200:
        raise RuntimeError(f"GitHub returned {r.status_code} listing {repo}")
    return r.json()


def recent(pr: dict, now: datetime | None = None) -> bool:
    try:
        updated = datetime.fromisoformat(pr["updated_at"].replace("Z", "+00:00"))
    except (KeyError, ValueError):
        return False
    return (now or datetime.now(timezone.utc)) - updated <= MAX_AGE


def targets() -> dict[str, str]:
    """Linked repos that have a coding bot -> the project owner whose GitHub login can read them if the App cannot."""
    db = SessionLocal()
    try:
        out: dict[str, str] = {}
        for project in db.query(Project).filter(Project.repo != "", Project.coding_bot_id != "", Project.pr_reviews.is_(True)).all():
            owner = db.query(ProjectMember.user_id).filter(ProjectMember.project_id == project.id, ProjectMember.role == "owner").scalar()
            out.setdefault(project.repo.lower(), owner or "")
        return out
    finally:
        db.close()


async def poll_once() -> int:
    """List open PRs on every linked repo and start a review for each new one. Returns how many were started."""
    from routes.webhooks import notify_coding_bots

    started = 0
    for repo, owner in (await asyncio.to_thread(targets)).items():
        try:
            prs = await open_prs(repo, await code_review._token(repo, owner))
        except Exception as exc:
            log.warning("Could not poll %s: %s", repo, exc)
            continue
        for pr in prs:
            if pr.get("draft") or not recent(pr):
                continue
            started += notify_coding_bots(repo, pr, "opened", only_new=True)
    return started


async def scheduler() -> None:
    await asyncio.sleep(30)  # let the server finish starting
    while True:
        try:
            if n := await poll_once():
                log.info("PR poll started %d review(s) the webhook missed", n)
        except Exception:
            log.exception("PR poll failed")
        await asyncio.sleep(INTERVAL)
