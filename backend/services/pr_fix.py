"""Fix the findings of a PR review: Muse changes the PR's branch in a sandbox, a person allows the push, and the fixes land as one commit on that branch.

Same shape as services/first_build.py. The findings are the review's fix-prompt document (made when the review finished); the sandbox gets the PR head, the
findings and the project skills; what comes back is a git patch (kept in Docs as a zip, so the Allow card can point at it), never a copy of the tree. Nothing is
pushed before the person says yes, and only to the PR's own branch in the same repository (a fork's branch is not ours to push to)."""
from __future__ import annotations

import asyncio
import logging
import re
import tempfile
import zipfile
from pathlib import Path

import httpx

from services import build_prompt, chat_rows, code_review as cr, runs
from services.build_publish import _find, _git
from services.build_sandbox import BuildBox
from services.first_build import BuildError, EFFORT, TIMEOUT, _need_sandbox, _slots

log = logging.getLogger(__name__)

_running: dict[tuple, asyncio.Task] = {}  # (project id, PR number) -> its fix run
PATCH_NAME = "fix.patch"


def _plan(project_id: str, user_id: str, repo: str, number: int) -> dict:
    """Who the fixing bot is and what it was told to fix (the review's fix prompt), or BuildError."""
    from core.database import SessionLocal
    from core.models import Agent, DocumentRecord, Project
    from tools.documents import document_path, user_may_read

    db = SessionLocal()
    try:
        project = db.get(Project, project_id)
        if project is None or (project.repo or "").lower() != repo.lower() or not project.coding_bot_id:
            raise BuildError("That pull request does not belong to a project with a coding bot.", 404)
        agent = db.get(Agent, project.coding_bot_id)
        docs = db.query(DocumentRecord).filter(DocumentRecord.project_id == project_id, DocumentRecord.filename.like(f"pr-{number}-fix-prompt%")).order_by(DocumentRecord.created_at.desc()).all()
        for doc in docs:
            path = document_path(doc.filename)
            if path is not None and path.is_file() and user_may_read(user_id, doc.filename, db=db):
                return {"agent_id": agent.id, "findings": path.read_text(encoding="utf-8", errors="replace")[:60_000], "project": project.name}
        raise BuildError("There are no review findings saved for that pull request.", 404)
    finally:
        db.close()


async def _pull(repo: str, number: int, token: str) -> dict:
    """The pull request as GitHub has it now, if it can be fixed by pushing to its branch."""
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.get(f"https://api.github.com/repos/{repo}/pulls/{number}", headers={"Authorization": "Bearer " + token, "Accept": "application/vnd.github+json"})
    if r.status_code != 200:
        raise BuildError(f"GitHub returned {r.status_code} for that pull request.", 502)
    pr = r.json()
    if pr.get("state") != "open":
        raise BuildError("That pull request is closed.", 409)
    if ((pr.get("head") or {}).get("repo") or {}).get("full_name", "").lower() != repo.lower():
        raise BuildError("That pull request comes from a fork, whose branch we cannot push to.", 409)
    return pr


async def start(project_id: str, user_id: str, repo: str, number: int) -> None:
    """The button was clicked. Checks at once what can be checked, puts the pill in the coding bot's chat, and fixes in the background."""
    key = (project_id, number)
    if (task := _running.get(key)) is not None and not task.done():
        raise BuildError("Fixes for that pull request are already being made.", 409)
    _need_sandbox()
    info = await asyncio.to_thread(_plan, project_id, user_id, repo, number)
    pr = await _pull(repo, number, await cr._token(repo, user_id))
    info["pr"] = pr
    pill = await asyncio.to_thread(chat_rows.write_row, info["agent_id"], user_id, _card(repo, pr, "running", stage="Starting"))
    task = runs.spawn(_fix_and_tell(project_id, user_id, repo, info, pill))
    _running[key] = task
    task.add_done_callback(lambda t, k=key: _running.get(k) is t and _running.pop(k, None))


def _card(repo: str, pr: dict, status: str, **more) -> str:
    return chat_rows.card("prfix", repo=repo, number=pr.get("number"), title=str(pr.get("title") or "")[:200], branch=(pr.get("head") or {}).get("ref", ""), status=status, **more)


async def fix(repo: str, user_id: str, pr: dict, findings: str, progress) -> dict:
    """Muse fixes the findings on the PR head, in a sandbox. Returns {patch, summary}; the patch is '' when Muse changed nothing."""
    from services.wiki import head_sha  # noqa: F401  (the PR's own head is used below)

    token = await cr._token(repo, user_id)
    tarball = await cr.fetch_tarball(repo, (pr.get("head") or {}).get("sha", ""), token)
    async with BuildBox(EFFORT, TIMEOUT) as box:
        await progress("Setting up the sandbox")
        await box.load(tarball, {**build_prompt.skill_files(), build_prompt.FINDINGS_PATH: findings})
        own_rules = await box.read("AGENTS.md")
        await box.write("AGENTS.md", build_prompt.with_rules(own_rules))
        await box.baseline()
        await progress("Muse is fixing the findings")
        summary = await box.build(build_prompt.pr_fix_prompt(repo, int(pr["number"]), str(pr.get("title") or "")))
        patch = await box.patch()
    return {"patch": patch, "summary": summary.strip()[-3000:]}


async def fix_live(session, repo: str, pr: dict, findings: str, progress) -> dict:
    """Muse fixes the findings in the running coding session, so the person watches the app change as it happens. The patch is what changed since the
    session's baseline (its start, or the last push). Raises RuntimeError when the session is busy with a change of its own."""
    from services.preview import _sync_stage

    if session.busy:
        raise RuntimeError("Muse is busy with another change in the live session. Try again when it has finished.")
    session.busy = True
    _sync_stage(session, "Muse is fixing the review findings…")
    try:
        async with session.lock:
            await progress("Muse is fixing the findings in the live build")
            await session.box.write(build_prompt.FINDINGS_PATH, findings)
            summary = await session.box.build(build_prompt.pr_fix_live_prompt(repo, int(pr["number"]), str(pr.get("title") or "")))
            patch = await session.box.patch()
    finally:
        session.busy = False
        _sync_stage(session, "Running")
    return {"patch": patch, "summary": summary.strip()[-3000:], "live": True}


def _save(project_id: str, user_id: str, agent_id: str, repo: str, pr: dict, patch: str) -> str:
    """The patch as a zip in Docs. Returns its file name."""
    from core.usage import reset_usage_user, set_usage_user
    from tools.documents import made_message, record, save_new

    def write(path: Path) -> None:
        with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
            z.writestr(PATCH_NAME, patch)
    acting = set_usage_user(user_id, project_id)
    try:
        name = save_new(f"pr-{pr['number']}-fixes", "zip", write)
        record(agent_id, made_message(f"PR #{pr['number']}", "zip", name), f"PR #{pr['number']} fixes (patch)", "The changes Muse made to fix the review findings, as a git patch.")
        return name
    finally:
        reset_usage_user(acting)


def _offer_push(project_id: str, user_id: str, agent_id: str, repo: str, pr: dict, archive: str, files: int) -> None:
    """Park the push behind the Allow/Deny card: it changes someone's branch."""
    from core.database import SessionLocal
    from core.usage import reset_usage_user, set_usage_user
    from tools.outbox import _park

    acting = set_usage_user(user_id, project_id)
    db = SessionLocal()
    try:
        call = {"tool": "push_pr_fix", "args": {"archive": archive, "number": pr["number"]}, "jira_key": "", "repo": repo}
        branch = (pr.get("head") or {}).get("ref", "")
        detail = f"One new commit on the branch {branch} of {repo} (pull request #{pr['number']}) with the fixes: {files} file(s) changed. The saved patch shows exactly what changes. Nothing else is touched."
        _park(agent_id, f"Push the fixes for #{pr['number']} to {branch} on {repo}", detail, call, db, interactive=True, title=f"Approve: fixes for PR #{pr['number']}")
    finally:
        db.close()
        reset_usage_user(acting)


def handover(repo: str, pr: dict, result: dict | None, failure: str, pushed_offer: bool) -> str:
    from services.review_render import untrusted_block

    head = f"You asked Muse to fix the review findings on PR #{pr.get('number')} of {repo}.\n"
    tail = "\nTell the person in a few casual sentences, no headings. You have no tools for this message."
    if result is None:
        return head + f"It did not finish: {failure or 'unknown error'}. They can press the fix button again." + tail
    if not result["patch"]:
        return head + "Muse changed nothing. Say what it reported:\n" + untrusted_block("Muse's closing message", result["summary"]) + tail
    where = "in the live build, so the preview beside this chat updated as it worked" if result.get("live") else "in a sandbox"
    return (head + f"Muse made the fixes and ran the tests {where}. " + ("A card in this chat asks them to allow pushing them to the PR's branch. " if pushed_offer else "")
            + "Say which findings it fixed and which it did not, and what it ran, from its report:\n" + untrusted_block("Muse's closing message", result["summary"]) + tail)


async def _fix_and_tell(project_id: str, user_id: str, repo: str, info: dict, pill: int) -> None:
    from routes.chat import _start_kickoff

    agent_id, pr = info["agent_id"], info["pr"]
    last = [""]

    async def progress(stage: str) -> None:
        if stage != last[0]:
            last[0] = stage
            await asyncio.to_thread(chat_rows.write_row, agent_id, user_id, _card(repo, pr, "running", stage=stage), pill)

    result, failure, offered, files = None, "", False, 0
    try:
        if _slots.locked():
            await progress("Waiting for another build to finish")
        from services import preview as live_preview

        session = live_preview.session_of(project_id, user_id)
        if session is not None and session.status == "running" and session.box is not None:  # the live build is up: the fixes are made there, where it can be watched
            result = await fix_live(session, repo, pr, info["findings"], progress)
        else:
            async with _slots:
                result = await fix(repo, user_id, pr, info["findings"], progress)
        if result["patch"]:
            files = len(re.findall(r"^diff --git ", result["patch"], re.M))
            archive = await asyncio.to_thread(_save, project_id, user_id, agent_id, repo, pr, result["patch"])
            try:
                await asyncio.to_thread(_offer_push, project_id, user_id, agent_id, repo, pr, archive, files)
                offered = True
            except Exception:
                log.exception("Could not offer the push for %s#%s", repo, pr.get("number"))
    except asyncio.CancelledError:
        await asyncio.to_thread(chat_rows.write_row, agent_id, user_id, _card(repo, pr, "failed", reason="Stopped: the server was restarting."), pill)
        raise
    except Exception as exc:
        log.exception("Fixing %s#%s failed", repo, pr.get("number"))
        failure = str(exc) if isinstance(exc, (RuntimeError, ValueError)) else type(exc).__name__
        result = None
    if result is None:
        card = _card(repo, pr, "failed", reason=failure[:300])
    elif not result["patch"]:
        card = _card(repo, pr, "done", files=0)
    else:
        card = _card(repo, pr, "done", files=files, waiting=offered)
    await asyncio.to_thread(chat_rows.write_row, agent_id, user_id, card, pill)
    _start_kickoff(agent_id, "Neuron", project_id, handover(repo, pr, result, failure, offered), user_id, no_tools=True)


# ── the push, after a person said yes ────────────────────────────────────

async def push(repo: str, user_id: str, agent_id: str, archive: str, number: int) -> str:
    """Apply the saved patch to the PR's branch and push one commit. Returns what to tell the person."""
    from services.wiki import _bot_identity

    path, _, project_id = await asyncio.to_thread(_find, agent_id, user_id, archive)
    with zipfile.ZipFile(path) as z:
        if z.namelist() != [PATCH_NAME] or z.getinfo(PATCH_NAME).file_size > 6 * 1024 * 1024:
            raise ValueError("That saved file is not a fix patch.")
        patch = z.read(PATCH_NAME)
    if re.search(rb"^(---|\+\+\+|rename (from|to)|copy (from|to)) [ab]?/?(\.git(/|$)|\.\./)", patch, re.M):
        raise ValueError("The patch touches git's own files or leaves the repository, so it was not applied.")
    token = await cr._token(repo, user_id)
    pr = await _pull(repo, number, token)
    branch = pr["head"]["ref"]
    author, email = await _bot_identity()
    with tempfile.TemporaryDirectory(prefix="prfix-") as tmp:
        work = Path(tmp) / "work"
        await _git("clone", "--depth", "1", "-b", branch, f"https://github.com/{repo}.git", "work", cwd=Path(tmp), token=token)
        if (await _git("rev-parse", "HEAD", cwd=work)).strip() != pr["head"]["sha"]:
            raise RuntimeError("The pull request has new commits since the fixes were made. Press the fix button again to fix the latest code.")
        (Path(tmp) / PATCH_NAME).write_bytes(patch)
        try:
            await _git("apply", "--index", "--whitespace=nowarn", str(Path(tmp) / PATCH_NAME), cwd=work)
        except RuntimeError:
            raise RuntimeError("The patch no longer applies to the branch. Press the fix button again.")
        changed = len((await _git("diff", "--cached", "--name-only", cwd=work)).split())
        if not changed:
            return f"Nothing to push: {branch} already has these changes."
        await _git("-c", f"user.name={author}", "-c", f"user.email={email}", "commit", "-q", "-m", f"Fix the review findings on #{number}", cwd=work)
        await _git("push", "-q", f"https://github.com/{repo}.git", f"HEAD:refs/heads/{branch}", cwd=work, token=token)
    from services import preview as live_preview

    live = live_preview.session_of(project_id, user_id)
    if live is not None and live.box is not None and live.status == "running":  # what is pushed is now the branch: the session measures its next change from here
        try:
            async with live.lock:
                await live.box.baseline()
        except Exception:
            log.exception("Could not move the live session's baseline after the push")
    note = f"Pushed one commit with the fixes for the review findings ({changed} files), made by Muse in a sandbox and allowed by a person. Please review it like any other commit."
    async with httpx.AsyncClient(timeout=30) as client:  # best effort: the push is what counts
        await client.post(f"https://api.github.com/repos/{repo}/issues/{number}/comments", headers={"Authorization": "Bearer " + token, "Accept": "application/vnd.github+json"}, json={"body": note})
    return f"Pushed the fixes to {branch} of {repo}: {changed} files in one commit. The pull request #{number} shows them."
