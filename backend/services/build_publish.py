"""Publish a project's first build as a pull request. Runs only after a person has allowed it on the approval card (it pushes a branch to their repository).

The code is the zip saved in Docs when the build finished, put over a fresh clone of the repository on a branch of its own. The default branch is not touched."""
from __future__ import annotations

import asyncio
import logging
import re
import shutil
import tempfile
import zipfile
from datetime import datetime
from pathlib import Path

import httpx

from services import code_review as cr

log = logging.getLogger(__name__)

MAX_FILES = 20_000
MAX_BYTES = 200 * 1024 * 1024
BODY_LIMIT = 30_000


def safe_extract(archive: Path, dest: Path) -> int:
    """Unpack a zip into dest and return how many files. A path that would leave dest, anything under .git, and links are skipped, and a zip that is
    too large is refused: the archive came out of a sandbox where a model wrote it, so it is read as untrusted."""
    count, total = 0, 0
    root = dest.resolve()
    with zipfile.ZipFile(archive) as zf:
        infos = zf.infolist()
        if len(infos) > MAX_FILES or sum(i.file_size for i in infos) > MAX_BYTES:
            raise RuntimeError("the saved code is too large to publish")
        for info in infos:
            name = info.filename
            parts = Path(name).parts
            if info.is_dir() or name.startswith("/") or ".." in parts or ".git" in parts or ((info.external_attr >> 16) & 0o170000) == 0o120000:
                continue
            target = (dest / name).resolve()
            if root not in target.parents:
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(info) as src, open(target, "wb") as out:
                shutil.copyfileobj(src, out)
            count += 1
            total += info.file_size
    return count


def _find(agent_id: str, user_id: str, name: str) -> tuple[Path, str, str]:
    """The saved archive (a path), and the project's name and id, if this person may read it and it belongs to the bot's project."""
    from core.database import SessionLocal
    from core.models import Agent, DocumentRecord, Project
    from tools.documents import document_path, user_may_read

    db = SessionLocal()
    try:
        agent = db.get(Agent, agent_id)
        rec = db.get(DocumentRecord, name)
        path = document_path(name)
        if agent is None or rec is None or path is None or path.suffix != ".zip" or not path.is_file() or rec.project_id != agent.project_id or not user_may_read(user_id, name, db=db):
            raise ValueError("The saved code could not be found on this project.")
        project = db.get(Project, agent.project_id)
        return path, (project.name if project else "the project"), agent.project_id
    finally:
        db.close()


def _guide(name: str) -> str:
    from tools.documents import document_path

    path = document_path(name) if name else None
    return path.read_text(encoding="utf-8", errors="replace")[:BODY_LIMIT] if path and path.is_file() else ""


async def _git(*args: str, cwd: Path, token: str = "", timeout: float = 300) -> str:
    try:
        rc, out, err = await cr.git_run(*args, cwd=str(cwd), token=token, timeout=timeout)
    except asyncio.TimeoutError:
        raise RuntimeError(f"git {args[0]} timed out")
    if rc != 0:
        log.error("git %s failed: %s", args[0], err[:300])
        raise RuntimeError(f"git {args[0]} failed")
    return out


async def publish(repo: str, user_id: str, agent_id: str, archive: str, guide: str = "") -> str:
    """Branch, commit, push and open the pull request. Returns what to tell the person, with the link."""
    from services.wiki import _bot_identity

    if not repo:
        raise ValueError("This project has no repository linked.")
    path, project, _ = await asyncio.to_thread(_find, agent_id, user_id, archive)
    token = await cr._token(repo, user_id)
    headers = {"Authorization": "Bearer " + token, "Accept": "application/vnd.github+json"}
    async with httpx.AsyncClient(timeout=30) as client:
        meta = await client.get(f"https://api.github.com/repos/{repo}", headers=headers)
    if meta.status_code != 200:
        raise RuntimeError(f"GitHub returned {meta.status_code} for the repository")
    base = meta.json()["default_branch"]
    branch = "neuron/first-build-" + datetime.now().strftime("%Y%m%d-%H%M%S")
    author, email = await _bot_identity()
    with tempfile.TemporaryDirectory(prefix="publish-") as tmp:
        work = Path(tmp) / "work"
        await _git("clone", "--depth", "1", f"https://github.com/{repo}.git", "work", cwd=Path(tmp), token=token)
        await _git("checkout", "-q", "-b", branch, cwd=work)
        files = await asyncio.to_thread(safe_extract, path, work)
        await _git("add", "-A", cwd=work)
        staged = await _git("diff", "--cached", "--name-only", cwd=work)
        if not staged.strip():
            return f"Nothing to publish: {repo} already has this code."
        await _git("-c", f"user.name={author}", "-c", f"user.email={email}", "commit", "-q", "-m", f"Add the first build of {project}", cwd=work)
        await _git("push", "-q", f"https://github.com/{repo}.git", f"HEAD:refs/heads/{branch}", cwd=work, token=token)
        changed = len(staged.split())
    body = (f"The first build of **{project}**, written by Muse in a sandbox from the plan the team and its owner agreed.\n\n"
            f"{changed} files changed ({files} in the archive). Nothing here has run outside that sandbox: follow the run guide below, and review it as you would any pull request.\n\n"
            + (f"---\n\n{_guide(guide)}\n" if guide else ""))
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.post(f"https://api.github.com/repos/{repo}/pulls", headers=headers, json={
            "title": f"First build of {project}", "head": branch, "base": base, "body": body[:BODY_LIMIT + 2000]})
    if r.status_code != 201:
        log.error("pull request failed: %s %s", r.status_code, r.text[:300])
        raise RuntimeError(f"The branch {branch} was pushed, but GitHub would not open the pull request ({r.status_code}). Open it from GitHub.")
    return f"Pull request opened: {r.json()['html_url']} ({changed} files on branch {branch})."
