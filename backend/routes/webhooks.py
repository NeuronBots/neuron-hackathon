"""Inbound webhooks. GitHub pull requests are handed to the project's coding bot."""
import asyncio
import hashlib
import hmac
import json
import logging
import os
from urllib.parse import parse_qs

import httpx
from fastapi import APIRouter, HTTPException, Request
from sqlalchemy import func

from core.database import SessionLocal
from core.models import Agent, Message, Project, ProjectMember, User
from integrations import connections
from services import code_review, followup, github_app, runs

router = APIRouter()
log = logging.getLogger(__name__)

# ready_for_review covers draft PRs: drafts are skipped on the other actions.
PR_ACTIONS = {"opened": "opened", "reopened": "reopened", "ready_for_review": "ready for review", "synchronize": "updated with new commits"}


def _signed(body: bytes, header: str) -> bool:
    secret = os.getenv("GITHUB_WEBHOOK_SECRET", "")
    if not secret:
        return False  # never accept unsigned deliveries
    want = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(want, header or "")


def _card(repo: str, pr: dict, what: str, **state) -> str:
    """The chat row for a review; the app shows it as a pill that opens to the PR. `state` is status (running, done, failed) and its details."""
    card = {
        "kind": "pr", "repo": repo, "number": pr.get("number"), "what": what, "title": str(pr.get("title") or "")[:200],
        "author": (pr.get("user") or {}).get("login", ""), "head": (pr.get("head") or {}).get("ref", ""),
        "base": (pr.get("base") or {}).get("ref", ""), "url": pr.get("html_url") or "", "sha": (pr.get("head") or {}).get("sha", ""),
        "status": "running", **state,
    }
    return "[[SOURCES:%s]]" % json.dumps(card)


_logins: dict[str, str] = {}  # user id -> GitHub login, learned from the account that user connected


async def _github_login(user_id: str) -> str:
    """Who this user is on GitHub, from their own connected account. Empty when they have not connected one."""
    if user_id in _logins:
        return _logins[user_id]
    token = await connections.access_token("github", user_id=user_id)  # None without a stored connection: the shared env token says nothing about them
    if not token:
        return ""
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.get("https://api.github.com/user", headers={"Authorization": "Bearer " + token, "Accept": "application/vnd.github+json"})
    except httpx.HTTPError:
        return ""
    login = (r.json().get("login") or "").lower() if r.status_code == 200 else ""
    if login:
        _logins[user_id] = login
    return login


async def _author(members: list[str], owner: str, login: str) -> str:
    """The project member who opened the PR, when that is not the owner (the owner gets everything anyway). Empty for an outside contributor."""
    for user_id in members:
        if login and user_id != owner and await _github_login(user_id) == login.lower():
            return user_id
    return ""


async def _pr_emails(repo: str, number: int, login: str, owner: str) -> set[str]:
    """The real email addresses of whoever opened the PR: on the commits they wrote, and on their public GitHub profile. GitHub's
    anonymous noreply addresses say nothing about who someone is, so they are left out."""
    token = await code_review._token(repo, owner)
    headers = {"Authorization": "Bearer " + token, "Accept": "application/vnd.github+json"}
    found: set[str] = set()
    async with httpx.AsyncClient(timeout=20) as client:
        r = await client.get(f"https://api.github.com/repos/{repo}/pulls/{number}/commits", params={"per_page": 100}, headers=headers)
        for c in (r.json() if r.status_code == 200 else []):
            if ((c.get("author") or {}).get("login") or "").lower() == login.lower():  # their own commits, not a co-author's
                found |= {c["commit"][who].get("email") or "" for who in ("author", "committer")}
        r = await client.get(f"https://api.github.com/users/{login}", headers=headers)
        found.add((r.json().get("email") or "") if r.status_code == 200 else "")
    return {e.lower() for e in found if e and "noreply" not in e.lower()}


def _member_with_email(members: list[str], owner: str, emails: set[str]) -> str:
    """The project member (not the owner, who gets everything anyway) whose Neuron email is one of these."""
    db = SessionLocal()
    try:
        for user in db.query(User).filter(User.id.in_(members), User.id != owner):
            if (user.email or "").lower() in emails:
                return user.id
        return ""
    finally:
        db.close()


async def _author_by_email(repo: str, number: int, login: str, owner: str, members: list[str]) -> str:
    """For an author with no GitHub connection in Neuron: the member whose email is on the author's commits or GitHub profile."""
    if not login:
        return ""
    return await asyncio.to_thread(_member_with_email, members, owner, await _pr_emails(repo, number, login, owner))


def seen_before(db, agent_id: str, owner: str, repo: str, number: int, sha: str) -> bool:
    """Does the owner's chat with the bot already show a review of this PR at this commit? A pill from before pills carried a commit counts
    as seen, so turning polling on does not review every PR that was already handled."""
    for (content,) in db.query(Message.content).filter(Message.agent_id == agent_id, Message.user_id == owner,
                                                      Message.content.like('[[SOURCES:{"kind": "pr"%'), Message.content.like(f'%"number": {number},%')):
        try:
            card = json.loads(content[len("[[SOURCES:"):-2])
        except ValueError:
            continue
        if card.get("number") == number and str(card.get("repo", "")).lower() == repo.lower() and card.get("sha", "") in ("", sha):
            return True
    return False


def last_reviewed_sha(agent_id: str, owner: str, repo: str, number: int) -> str:
    """The commit the owner's chat shows as most recently reviewed for this PR (a finished review), or '' when it was never reviewed."""
    db = SessionLocal()
    try:
        rows = db.query(Message.content).filter(Message.agent_id == agent_id, Message.user_id == owner, Message.content.like('[[SOURCES:{"kind": "pr"%'),
                                                Message.content.like(f'%"number": {number},%')).order_by(Message.id.desc())
        for (content,) in rows:
            try:
                card = json.loads(content[len("[[SOURCES:"):-2])
            except ValueError:
                continue
            if card.get("number") == number and str(card.get("repo", "")).lower() == repo.lower() and card.get("status") == "done" and card.get("sha"):
                return card["sha"]
        return ""
    finally:
        db.close()


def _write_row(agent_id: str, owner: str, text: str, row_id: int | None = None) -> int:
    """Add a row to the owner's chat with the bot, or overwrite row_id in place. Returns the row's id."""
    db = SessionLocal()
    try:
        row = db.get(Message, row_id) if row_id else None
        if row is None:
            row = Message(agent_id=agent_id, user_id=owner, role="assistant", content=text)
            db.add(row)
        row.content = text
        db.commit()
        return row.id
    finally:
        db.close()


async def _review_and_tell(agent_id: str, project_id: str, owner: str, repo: str, pr: dict, what: str, card_id: int, bot_name: str, members: list[str], context: str, since: str) -> None:
    """The review, in the background: the pill spins until it ends, then shows the outcome. The review is saved as a document, posted on
    the PR when that is switched on, and the coding bot tells the owner how it went."""
    from routes.chat import _start_kickoff
    from tools.documents import document_url

    result, doc_row, doc_rows = None, "", []
    login = (pr.get("user") or {}).get("login", "")
    author = await _author(members, owner, login)  # whoever opened the PR gets the same messages in their own chat, found by their GitHub connection
    if not author:
        try:
            author = await _author_by_email(repo, int(pr["number"]), login, owner, members)  # or by the email on their commits
        except Exception:
            log.exception("Could not look up who opened %s#%s", repo, pr.get("number"))
    cards = {owner: card_id}
    if author:
        cards[author] = await asyncio.to_thread(_write_row, agent_id, author, _card(repo, pr, what, **({"followup": True} if since else {})))
    try:
        result = None
        if since:  # new commits after a finished review: check the earlier findings, review what changed
            result = await followup.review(repo, pr, owner, context, since)
        if result is None:
            result = await code_review.review(repo, pr, owner, context)
        try:
            result["doc"], name = await asyncio.to_thread(code_review.publish_doc, agent_id, project_id, owner, members, repo, pr, result, bot_name)
            doc_row = f"[{result['doc']}]({document_url(name)})"
        except Exception:
            log.exception("Could not save the review document for %s#%s", repo, pr.get("number"))
        try:  # the fix prompt is a document of its own, so it can be opened and handed to a coding agent
            fix = await asyncio.to_thread(code_review.publish_fix_doc, agent_id, project_id, owner, members, repo, pr, result)
            if fix:
                doc_rows.append(f"[{fix[0]}]({document_url(fix[1])})")
        except Exception:
            log.exception("Could not save the fix prompt for %s#%s", repo, pr.get("number"))
        if os.getenv("CODE_REVIEW_POST") == "1" and github_app.configured() and (pr.get("head") or {}).get("sha"):
            try:
                await (github_app.post_followup if result.get("followup") else github_app.post_review)(repo, int(pr["number"]), pr, result)
                result["posted"] = True
            except Exception:
                log.exception("Could not post the review of %s#%s", repo, pr.get("number"))
        state = {"status": "done", "verdict": result["verdict"], "findings": len(result["findings"]), "posted": bool(result.get("posted")),
                 "uncovered": len(result.get("not_reviewed") or [])}
        if result.get("followup"):
            state.update(followup=True, fixed=len(result["fixed"]), open=len(result["still_open"]))
        text = followup.handover(repo, pr, result) if result.get("followup") else code_review.handover(repo, pr, result)
    except Exception as exc:
        log.exception("Review of %s#%s failed", repo, pr.get("number"))
        failure = str(exc) if isinstance(exc, RuntimeError) else type(exc).__name__
        state, text = {"status": "failed", "reason": failure[:200]}, code_review.handover(repo, pr, None, failure)
    for user_id, row_id in cards.items():
        await asyncio.to_thread(_write_row, agent_id, user_id, _card(repo, pr, what, **state), row_id)
        for row in ([doc_row] if doc_row else []) + doc_rows:  # a row of its own, shown as a file card like any document a bot makes; it also makes the open chat refresh
            await asyncio.to_thread(_write_row, agent_id, user_id, row)

    def tell_author(reply: str, failure: str) -> None:
        if author and reply.strip():  # the bot's words to the owner, as they were said: one model run, two chats
            _write_row(agent_id, author, reply.strip())
    _start_kickoff(agent_id, "GitHub", project_id, text, owner, on_done=tell_author)


def notify_coding_bots(repo: str, pr: dict, what: str, only_new: bool = False) -> int:
    """Start the coding bot of every project linked to `repo`. Returns how many were started. With only_new (the poll), a PR that already
    has a review pill for this commit is left alone; a webhook is an explicit event, so it always reviews."""
    from integrations import notifications

    db = SessionLocal()
    try:
        projects = db.query(Project).filter(func.lower(Project.repo) == repo.lower(), Project.coding_bot_id != "", Project.pr_reviews.is_(True)).all()
        started = 0
        for project in projects:
            agent = db.get(Agent, project.coding_bot_id)
            if agent is None or agent.project_id != project.id:
                continue  # the bot was deleted
            members = db.query(ProjectMember).filter(ProjectMember.project_id == project.id).all()
            owner = next((m.user_id for m in members if m.role == "owner"), members[0].user_id if members else "")
            if not owner:
                continue
            if only_new and seen_before(db, agent.id, owner, repo, int(pr.get("number") or 0), (pr.get("head") or {}).get("sha", "")):
                continue
            if not code_review.claim(project.id, repo, int(pr.get("number") or 0), (pr.get("head") or {}).get("sha", "")):
                continue  # a redelivery of a commit that is already being reviewed
            sha = (pr.get("head") or {}).get("sha", "")
            since = last_reviewed_sha(agent.id, owner, repo, int(pr.get("number") or 0))
            since = since if since != sha else ""  # the same commit again is a fresh review, not a follow-up
            card_id = _write_row(agent.id, owner, _card(repo, pr, what, **({"followup": True} if since else {})))
            runs.spawn(_review_and_tell(agent.id, project.id, owner, repo, pr, what, card_id, agent.name, [m.user_id for m in members],
                                       " ".join(x for x in (project.name, project.goal, project.summary) if x)[:1500], since))
            asyncio.get_running_loop().run_in_executor(
                None, notifications.notify, [m.user_id for m in members], f"PR #{pr.get('number')} {what}", str(pr.get("title") or ""), pr.get("html_url") or "/"
            )
            started += 1
        return started
    finally:
        db.close()


@router.post("/api/webhooks/github")
async def github_webhook(request: Request):
    body = await request.body()
    if not _signed(body, request.headers.get("X-Hub-Signature-256", "")):
        raise HTTPException(status_code=401, detail="Bad signature")
    if request.headers.get("X-GitHub-Event") != "pull_request":
        return {"ok": True}  # ping and every other event
    try:
        if "x-www-form-urlencoded" in request.headers.get("Content-Type", ""):  # GitHub's default content type: the JSON is in a `payload` field
            body = parse_qs(body.decode()).get("payload", [""])[0].encode()
        payload = json.loads(body)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid payload")
    what = PR_ACTIONS.get(payload.get("action", ""))
    pr = payload.get("pull_request") or {}
    repo = (payload.get("repository") or {}).get("full_name") or ""
    if not what or not repo or not pr:
        return {"ok": True}
    if pr.get("draft") and payload.get("action") != "ready_for_review":
        return {"ok": True, "skipped": "draft"}
    return {"ok": True, "notified": notify_coding_bots(repo, pr, what)}
