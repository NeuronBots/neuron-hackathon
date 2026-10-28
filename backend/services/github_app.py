"""The GitHub App that posts review comments, so they come from the bot and not from whoever linked the repo."""
from __future__ import annotations

import calendar
import functools
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


class _Unauthorized(RuntimeError):
    """GitHub answered 401: the cached installation token is no good (revoked, or expired early)."""


def _check(r: httpx.Response) -> httpx.Response:
    if r.status_code == 401:
        raise _Unauthorized("GitHub rejected the token")
    return r


def _retry_on_401(fn):
    """Run an App call; if GitHub rejects the cached token, drop it and run the call once more with a fresh one."""
    @functools.wraps(fn)
    async def wrapper(repo, *args, **kwargs):
        try:
            return await fn(repo, *args, **kwargs)
        except _Unauthorized:
            _tokens.pop(repo.lower(), None)
            return await fn(repo, *args, **kwargs)
    return wrapper


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
    expires = calendar.timegm(time.strptime(data["expires_at"], "%Y-%m-%dT%H:%M:%SZ"))  # the string is UTC; mktime would read it as local time
    _tokens[repo.lower()] = (expires, data["token"])
    return data["token"]


_slug = ""
_THREADS = """query($owner: String!, $name: String!, $number: Int!, $after: String) { repository(owner: $owner, name: $name) { pullRequest(number: $number) {
  reviewThreads(first: 100, after: $after) { pageInfo { hasNextPage endCursor }
    nodes { id isResolved path line originalLine comments(first: 1) { nodes { body author { login } } } } } } } }"""
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


@_retry_on_401
async def open_bot_threads(repo: str, number: int) -> list[dict]:
    """The App's own review threads on the PR that nobody has resolved: what the last review left open. Every page is read: a PR with a long
    review history has more than 100 threads, and the open ones are not necessarily among the first."""
    token, me = await installation_token(repo), await bot_login()
    owner, name = repo.split("/", 1)
    out, after = [], None
    async with httpx.AsyncClient(timeout=30) as client:
        for _ in range(50):  # 5000 threads
            r = _check(await client.post(f"{API}/graphql", headers=_headers(token),
                                         json={"query": _THREADS, "variables": {"owner": owner, "name": name, "number": number, "after": after}}))
            if r.status_code != 200 or "errors" in r.json():
                raise RuntimeError("GitHub would not list the review threads")
            threads = r.json()["data"]["repository"]["pullRequest"]["reviewThreads"]
            for node in threads["nodes"]:
                first = (node["comments"]["nodes"] or [{}])[0]
                if not node["isResolved"] and (first.get("author") or {}).get("login") == me:
                    out.append({"id": node["id"], "path": node["path"], "line": node.get("line") or node.get("originalLine") or 0, "body": first.get("body", "")})
            if not threads["pageInfo"]["hasNextPage"]:
                break
            after = threads["pageInfo"]["endCursor"]
    return out


@_retry_on_401
async def resolve_threads(repo: str, thread_ids: list[str]) -> int:
    """Close the threads of findings that were fixed. One failure does not stop the rest. Returns how many were resolved."""
    token, done = await installation_token(repo), 0
    async with httpx.AsyncClient(timeout=30) as client:
        for thread_id in thread_ids:
            r = _check(await client.post(f"{API}/graphql", headers=_headers(token), json={"query": _RESOLVE, "variables": {"id": thread_id}}))
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


async def _comment(client: httpx.AsyncClient, token: str, repo: str, number: int, body: str) -> None:
    r = _check(await client.post(f"{API}/repos/{repo}/issues/{number}/comments", headers=_headers(token), json={"body": body[:65000]}))
    if r.status_code >= 300:
        raise RuntimeError(f"GitHub refused a comment ({r.status_code})")


async def _post_one_review(client: httpx.AsyncClient, token: str, repo: str, number: int, sha: str, comments: list[dict], summary, findings: list[dict], loose: list[dict]) -> int:
    """Post the review: the inline comments with the summary as its body. GitHub answers 422 for the whole review when any one comment
    cannot sit where it is anchored; then everything goes into the body of a single review instead, so nothing is lost and no
    half-posted state is left. Any other refusal is an error. Returns how many comments sat inline."""
    url = f"{API}/repos/{repo}/pulls/{number}/reviews"
    r = _check(await client.post(url, headers=_headers(token), json={"commit_id": sha, "event": "COMMENT", "comments": comments, "body": summary(len(comments), loose)}))
    inline = len(comments)
    if r.status_code == 422 and comments:
        inline = 0
        body = summary(0, loose + [f for f in findings if f not in loose]) + "\n\n" + render.findings_md(findings)
        r = _check(await client.post(url, headers=_headers(token), json={"commit_id": sha, "event": "COMMENT", "body": body[:65000]}))
    if r.status_code >= 300:
        raise RuntimeError(f"GitHub refused the review ({r.status_code})")
    return inline


@_retry_on_401
async def post_followup(repo: str, number: int, pr: dict, result: dict) -> int:
    """A follow-up review on GitHub: new issues go inline, the summary is the review body, then the threads of fixed findings are resolved
    (only once the review is posted, so a failed post never leaves threads closed with no explanation), and a fix prompt covers what is
    still open. Returns how many new comments sat inline."""
    token = await installation_token(repo)
    sha = pr["head"]["sha"]
    comments, loose = render.anchor(result["findings"], render.commentable_lines(result.get("diff", "")))
    async with httpx.AsyncClient(timeout=60) as client:
        inline = await _post_one_review(client, token, repo, number, sha, comments, lambda n, l: render.followup_summary_md(result, n, l, sha), result["findings"], loose)
        if result["fixed_threads"]:
            await resolve_threads(repo, result["fixed_threads"])
        prompt = render.fix_prompt(repo, pr, result["still_open"] + result["findings"])
        if prompt:
            await _comment(client, token, repo, number, f"**Copy-paste this into a coding agent to fix the findings:**\n\n{render.fence(prompt)}")
    return inline


@_retry_on_401
async def post_review(repo: str, number: int, pr: dict, result: dict) -> int:
    """What the original bot left on a PR, now from the App: a walkthrough comment, one inline review with the summary as its body, and
    a fix-prompt comment. Returns how many comments sat inline."""
    token = await installation_token(repo)
    sha = pr["head"]["sha"]
    comments, loose = render.anchor(result["findings"], render.commentable_lines(result.get("diff", "")))
    async with httpx.AsyncClient(timeout=60) as client:
        if result.get("walkthrough"):
            try:
                await _comment(client, token, repo, number, render.walkthrough_md(result["walkthrough"]))
            except _Unauthorized:
                raise
            except RuntimeError:
                log.exception("Could not post the walkthrough on %s#%s", repo, number)
        inline = await _post_one_review(client, token, repo, number, sha, comments, lambda n, l: render.summary_md(result, n, l, sha), result["findings"], loose)
        prompt = render.fix_prompt(repo, pr, result["findings"])
        if prompt:
            await _comment(client, token, repo, number, f"**Copy-paste this into a coding agent to fix the findings:**\n\n{render.fence(prompt)}")
    return inline
