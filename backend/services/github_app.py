"""The GitHub App that posts review comments, so they come from the bot and not from whoever linked the repo."""
from __future__ import annotations

import logging
import os
import time
from pathlib import Path

import httpx
import jwt

from services import review_render as render

log = logging.getLogger(__name__)
API = "https://api.github.com"
_tokens: dict[str, tuple[float, str]] = {}  # repo -> (expires at, installation token)


def configured() -> bool:
    return bool(os.getenv("GITHUB_APP_ID") and Path(os.getenv("GITHUB_APP_PRIVATE_KEY_PATH", "")).is_file())


def _app_jwt() -> str:
    now = int(time.time())
    key = Path(os.environ["GITHUB_APP_PRIVATE_KEY_PATH"]).read_text()
    return jwt.encode({"iat": now - 30, "exp": now + 540, "iss": os.environ["GITHUB_APP_ID"]}, key, algorithm="RS256")


def _headers(token: str) -> dict:
    return {"Authorization": "Bearer " + token, "Accept": "application/vnd.github+json"}


async def installation_token(repo: str) -> str:
    """A token for the App's installation on this repo. Raises RuntimeError when the App is not installed there."""
    hit = _tokens.get(repo.lower())
    if hit and hit[0] > time.time() + 60:
        return hit[1]
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.get(f"{API}/repos/{repo}/installation", headers=_headers(_app_jwt()))
        if r.status_code != 200:
            raise RuntimeError(f"the GitHub App is not installed on {repo}")
        r = await client.post(f"{API}/app/installations/{r.json()['id']}/access_tokens", headers=_headers(_app_jwt()))
        r.raise_for_status()
    data = r.json()
    expires = time.mktime(time.strptime(data["expires_at"], "%Y-%m-%dT%H:%M:%SZ")) - time.timezone
    _tokens[repo.lower()] = (expires, data["token"])
    return data["token"]


_slug = ""
_THREADS = """query($owner: String!, $name: String!, $number: Int!) { repository(owner: $owner, name: $name) { pullRequest(number: $number) {
  reviewThreads(first: 100) { nodes { id isResolved path line originalLine comments(first: 1) { nodes { body author { login } } } } } } } }"""
_RESOLVE = "mutation($id: ID!) { resolveReviewThread(input: {threadId: $id}) { thread { id } } }"


async def bot_login() -> str:
    """The App's own login, as its comments are signed on GitHub."""
    global _slug
    if not _slug:
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.get(f"{API}/app", headers=_headers(_app_jwt()))
        r.raise_for_status()
        _slug = r.json()["slug"]
    return _slug


async def open_bot_threads(repo: str, number: int) -> list[dict]:
    """The App's own review threads on the PR that nobody has resolved: what the last review left open."""
    token, me = await installation_token(repo), await bot_login()
    owner, name = repo.split("/", 1)
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.post(f"{API}/graphql", headers=_headers(token), json={"query": _THREADS, "variables": {"owner": owner, "name": name, "number": number}})
    if r.status_code != 200 or "errors" in r.json():
        raise RuntimeError("GitHub would not list the review threads")
    out = []
    for node in r.json()["data"]["repository"]["pullRequest"]["reviewThreads"]["nodes"]:
        first = (node["comments"]["nodes"] or [{}])[0]
        if not node["isResolved"] and (first.get("author") or {}).get("login") == me:
            out.append({"id": node["id"], "path": node["path"], "line": node.get("line") or node.get("originalLine") or 0, "body": first.get("body", "")})
    return out


async def resolve_threads(repo: str, thread_ids: list[str]) -> int:
    """Close the threads of findings that were fixed. One failure does not stop the rest. Returns how many were resolved."""
    token, done = await installation_token(repo), 0
    async with httpx.AsyncClient(timeout=30) as client:
        for thread_id in thread_ids:
            r = await client.post(f"{API}/graphql", headers=_headers(token), json={"query": _RESOLVE, "variables": {"id": thread_id}})
            if r.status_code == 200 and "errors" not in r.json():
                done += 1
            else:
                log.warning("Could not resolve review thread %s on %s", thread_id, repo)
    return done


async def compare_files(repo: str, base: str, head: str, token: str) -> list[dict] | None:
    """Files changed between two commits, shaped like the PR's own file list. None when GitHub cannot say (a force-push removed the base,
    or the change is too big to list), so the caller reviews the whole PR instead."""
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.get(f"{API}/repos/{repo}/compare/{base}...{head}", headers=_headers(token))
    if r.status_code != 200:
        return None
    files = r.json().get("files") or []
    if len(files) >= 300:
        return None
    return [{"path": f["filename"], "status": f["status"], "patch": f.get("patch") or "", "add": f.get("additions", 0), "del": f.get("deletions", 0)} for f in files]


async def post_followup(repo: str, number: int, pr: dict, result: dict) -> int:
    """A follow-up review on GitHub: threads of fixed findings are resolved, new issues go inline, the summary is the review body, and a
    fix prompt covers what is still open. Returns how many new comments sat inline."""
    token = await installation_token(repo)
    sha = pr["head"]["sha"]
    url = f"{API}/repos/{repo}/pulls/{number}/reviews"
    if result["fixed_threads"]:
        await resolve_threads(repo, result["fixed_threads"])
    comments, loose = render.anchor(result["findings"], render.commentable_lines(result.get("diff", "")))
    inline = len(comments)
    async with httpx.AsyncClient(timeout=60) as client:
        r = await client.post(url, headers=_headers(token), json={"commit_id": sha, "event": "COMMENT", "comments": comments,
                                                                 "body": render.followup_summary_md(result, inline, loose, sha)})
        if r.status_code == 422 and comments:  # one bad anchor fails the whole review: the findings go in the summary instead
            loose, inline = loose + result["findings"], 0
            r = await client.post(url, headers=_headers(token), json={"commit_id": sha, "event": "COMMENT", "body": render.followup_summary_md(result, 0, loose, sha)})
        if r.status_code >= 300:
            raise RuntimeError(f"GitHub refused the review ({r.status_code})")
        prompt = render.fix_prompt(repo, pr, result["still_open"] + result["findings"])
        if prompt:
            await _comment(client, token, repo, number, f"**Copy-paste this into a coding agent to fix the findings:**\n\n{render.fence(prompt)}")
    return inline


async def _comment(client: httpx.AsyncClient, token: str, repo: str, number: int, body: str) -> None:
    r = await client.post(f"{API}/repos/{repo}/issues/{number}/comments", headers=_headers(token), json={"body": body[:65000]})
    if r.status_code >= 300:
        raise RuntimeError(f"GitHub refused a comment ({r.status_code})")


async def post_review(repo: str, number: int, pr: dict, result: dict) -> int:
    """What the original bot left on a PR, now from the App: a walkthrough comment, one inline review with the summary as its body, and
    a fix-prompt comment. Each step stands alone, so one refusal does not lose the rest. Returns how many comments sat inline."""
    token = await installation_token(repo)
    sha = pr["head"]["sha"]
    url = f"{API}/repos/{repo}/pulls/{number}/reviews"
    comments, loose = render.anchor(result["findings"], render.commentable_lines(result.get("diff", "")))
    inline = 0
    async with httpx.AsyncClient(timeout=60) as client:
        if result.get("walkthrough"):
            try:
                await _comment(client, token, repo, number, render.walkthrough_md(result["walkthrough"]))
            except RuntimeError:
                log.exception("Could not post the walkthrough on %s#%s", repo, number)
        r = await client.post(url, headers=_headers(token), json={"commit_id": sha, "event": "COMMENT", "comments": comments,
                                                                 "body": render.summary_md(result, len(comments), loose, sha)})
        if r.status_code == 422 and comments:  # one bad anchor fails the whole review: post what can be posted one at a time
            kept = []
            for c in comments:
                one = await client.post(url, headers=_headers(token), json={"commit_id": sha, "event": "COMMENT", "comments": [c]})
                kept.append(c) if one.status_code < 300 else loose.append(next(f for f in result["findings"] if f["title"] in c["body"]))
            inline = len(kept)
            r = await client.post(url, headers=_headers(token), json={"commit_id": sha, "event": "COMMENT", "body": render.summary_md(result, inline, loose, sha)})
        else:
            inline = len(comments) if r.status_code < 300 else 0
        if r.status_code >= 300:
            raise RuntimeError(f"GitHub refused the review ({r.status_code})")
        prompt = render.fix_prompt(repo, pr, result["findings"])
        if prompt:
            await _comment(client, token, repo, number, f"**Copy-paste this into a coding agent to fix the findings:**\n\n{render.fence(prompt)}")
    return inline
