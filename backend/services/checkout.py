"""After payments are turned on: Muse adds checkout to the built site, in the project's coding session, and the result is saved and offered for publishing."""
from __future__ import annotations

import asyncio
import logging
import time

from services import build_prompt

log = logging.getLogger(__name__)


async def _until(ready, seconds: float) -> bool:
    """Wait for `ready()`, up to `seconds`. False when the time ran out."""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if ready():
            return True
        await asyncio.sleep(2)
    return False


PRICES_FILE = "neuron-prices.json"


async def _propose_prices(project_id: str, session) -> None:
    """Muse lists what the page sells in a file; it is read, kept for the owner to approve in the Wallet, and removed so it is never saved with the site."""
    from core.database import SessionLocal
    from core.models import Project
    from services import payments

    raw = await session.box.read(PRICES_FILE)
    if raw is None:
        return
    await session.box.write(PRICES_FILE, None)

    def keep() -> int:
        db = SessionLocal()
        try:
            project = db.get(Project, project_id)
            return payments.propose_prices(db, project, raw) if project else 0
        finally:
            db.close()

    kept = await asyncio.to_thread(keep)
    log.info("payments for %s: %d prices proposed", project_id, kept)
    if kept:
        await asyncio.to_thread(_ask_owner, project_id, session.agent_id)


def _ask_owner(project_id: str, agent_id: str) -> None:
    """Nothing can be paid for until the owner approves the prices: put it where they will see it (the board, and a push when push is set up)."""
    from core.database import SessionLocal
    from tools.outbox import _queue_action, notify_pending

    title = "Approve the prices Muse found: Wallet, Prices, Use these prices, Save prices"
    db = SessionLocal()
    try:
        _queue_action(agent_id, "Blocked", title, "Muse added checkout to the site and listed what it sells. Until you save a price list in your Wallet, customers cannot pay: the amount charged always comes from that list.", None, db)
        notify_pending(agent_id, title, db)
    finally:
        db.close()


async def add_checkout(project_id: str, user_id: str, agent_id: str) -> None:
    """Never raises: it runs in the background, and the person is told in the chat what came of it. Saving and publishing still wait for their Allow."""
    from core.usage import reset_usage_user, set_usage_user
    from services import first_build as fb, preview

    acting = set_usage_user(user_id, project_id)
    try:
        _, name, _, archive = await asyncio.to_thread(preview._context, agent_id)
        session = preview.session_of(project_id, user_id)
        if session is None:
            if not archive:
                log.warning("payments for %s: there is no saved build to add checkout to", project_id)
                return
            await preview.start_session(project_id, name, user_id, agent_id, archive)
            session = preview.session_of(project_id, user_id)
        if session is None or not await _until(lambda: session.status == "running" and not session.busy, 900):
            log.warning("payments for %s: the coding session did not come up", project_id)
            return
        told = await preview.change_for_bot(agent_id, build_prompt.payments_request(name))
        if told.startswith("Error"):
            log.warning("payments for %s: %s", project_id, told)
            return
        await asyncio.sleep(2)  # change_for_bot marks the session busy before it answers
        await _until(lambda: not session.busy or session.status != "running", 1800)
        if session.status != "running":
            return
        await _propose_prices(project_id, session)
        saved = await preview.save(session)  # a new zip in Docs; a repository's pull request waits for its own Allow
        await asyncio.to_thread(fb._offer_publish, project_id, user_id, agent_id, name, {"zip": saved["zip"]})  # putting it on the public site waits for an Allow too
    except Exception:
        log.exception("adding checkout to %s failed", project_id)
    finally:
        reset_usage_user(acting)
