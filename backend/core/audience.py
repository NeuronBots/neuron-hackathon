"""Who else can read a bot's answer, and which of the asker's connected accounts they cannot see."""
import asyncio
import re
import time
from contextvars import ContextVar, Token
from dataclasses import dataclass
from urllib.parse import quote

import httpx


@dataclass(frozen=True)
class Requester:
    """Who a tool call in a shared chat acts for: the person who asked, in which chat, with whom."""
    user_id: str
    chat_id: str
    participants: tuple[str, ...]


_requester: ContextVar[Requester | None] = ContextVar("requester", default=None)
_audience: ContextVar[tuple[str, ...]] = ContextVar("audience", default=())
_withheld: ContextVar[set[str] | None] = ContextVar("withheld", default=None)  # set per turn; child tasks share it, so tools running in them report back
_actions: ContextVar[list[dict] | None] = ContextVar("actions", default=None)  # writes held for the asker's Proceed this turn; shared like _withheld
_approved: ContextVar[bool] = ContextVar("approved", default=False)  # True while the asker's own Proceed is being carried out


def set_audience(user_ids: list[str], chat_id: str = "", approved: bool = False) -> tuple[Token, ...]:
    """The other people reading this turn's answer, and the requester context for its tool calls. Returns tokens for reset_audience.
    `approved` is for carrying out a write the asker has just approved: it runs, instead of being held again."""
    from core.usage import current_user

    asker = current_user()
    return (_audience.set(tuple(user_ids)), _withheld.set(set()),
            _requester.set(Requester(asker, chat_id, (asker, *user_ids))), _actions.set([]), _approved.set(approved))


def reset_audience(tokens: tuple[Token, ...]) -> None:
    for var, token in zip((_audience, _withheld, _requester, _actions, _approved), tokens):
        var.reset(token)


def hold_write(provider: str, name: str, arguments: dict, jira_key: str, repo: str, agent_id: str) -> str:
    """In a shared chat a write through the asker's account waits for their Proceed. Returns what the bot is told, or "" when the call may run."""
    from tools.writes import is_write

    held = _actions.get()
    if held is None or _approved.get() or not is_write(name):
        return ""
    call = {"provider": provider, "tool": name, "args": arguments, "jira_key": jira_key, "repo": repo, "agent_id": agent_id}
    if call not in held:
        held.append(call)
    return ("NOT done yet: this changes something in the asker's account, so they must approve it first. They will see what you prepared with a Proceed button. "
            "Say in one sentence what you prepared. Do not call it again and do not say it is done.")


def actions() -> list[dict]:
    """The writes held this turn."""
    return list(_actions.get() or ())


def requester() -> Requester | None:
    """The requester context of this tool call; None outside a shared chat."""
    return _requester.get()


def readers() -> tuple[str, ...]:
    """The other people reading this turn's answer; empty outside a shared chat."""
    return _audience.get()


def note(provider: str) -> None:
    """This turn used something the readers may not see, so the answer waits for the asker's say-so."""
    held = _withheld.get()
    if _audience.get() and held is not None:
        held.add(provider)


def withheld() -> list[str]:
    return sorted(_withheld.get() or ())


_URL = re.compile(r"https?://[^\s<>\"')\]]*[^\s<>\"')\].,;:!?]")  # stops before the punctuation that ends a sentence
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
_ISSUE_KEY = re.compile(r"\b[A-Z][A-Z0-9]{1,9}-\d+\b")
_HEX_ID = re.compile(r"\b(?=[0-9a-f]*\d)[0-9a-f]{8,}\b", re.IGNORECASE)  # uuids and commit shas


def scrub(text: str, limit: int = 600) -> str:
    """Take links, email addresses and ids out of text bound for someone else, and cap its length. A floor under what a model was asked to leave out."""
    for pattern, label in ((_URL, "[link]"), (_EMAIL, "[email]"), (_ISSUE_KEY, "[id]"), (_HEX_ID, "[id]")):
        text = pattern.sub(label, text)
    text = " ".join(text.split())
    return text if len(text) <= limit else text[:limit].rstrip() + "…"


PERSONAL = ("gmail", "private chats")  # never shared, whatever the reader has connected
_REPO = re.compile(r"^(?!\.{1,2}/)(?!.*/\.{1,2}$)[\w.-]+/[\w.-]+$")  # owner/name, and neither part may be "." or ".."
_JIRA_KEY = re.compile(r"^[A-Z][A-Z0-9_]{1,19}$")
_access: dict[tuple, tuple[float, bool]] = {}  # (provider, user, scope) -> (expires, can read it)
_ACCESS_TTL = 60  # a revoked permission is honoured within a minute
_ACCESS_MAX = 256


async def _checked(key: tuple, check) -> bool:
    """The cached outcome of an access check. Fails closed on any error."""
    now = time.time()
    hit = _access.get(key)
    if hit and hit[0] > now:
        return hit[1]
    try:
        ok = bool(await check())
    except Exception:
        ok = False  # unknown means no access
    if len(_access) >= _ACCESS_MAX:
        for stale in [k for k, (exp, _) in _access.items() if exp <= now] or list(_access)[: _ACCESS_MAX // 2]:
            _access.pop(stale, None)
    _access[key] = (now + _ACCESS_TTL, ok)
    return ok


async def _get_ok(url: str, token: str, accept: str) -> bool:
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.get(url, headers={"Authorization": f"Bearer {token}", "Accept": accept})
    return resp.status_code == 200


async def _can_read_repo(uid: str, repo: str) -> bool:
    """True when this person's own GitHub token can open the repo. The server's token is never used for a reader."""
    from integrations import connections

    async def check() -> bool:
        if not _REPO.match(repo):
            return False
        token = await connections.access_token("github", uid)
        return bool(token) and await _get_ok("https://api.github.com/repos/" + quote(repo, safe="/"), token, "application/vnd.github+json")

    return await _checked(("github", uid, repo), check)


async def _can_read_jira(uid: str, key: str) -> bool:
    """True when this person's own Atlassian token can open the project's Jira project on its site."""
    from core.database import SessionLocal
    from core.models import Project
    from core.usage import current_project
    from integrations import connections

    def cloud() -> str:
        db = SessionLocal()
        try:
            project = db.get(Project, current_project())
            return (project.jira_cloud_id if project else "") or ""
        finally:
            db.close()

    async def check() -> bool:
        site = await asyncio.to_thread(cloud)
        if not _JIRA_KEY.match(key) or not re.match(r"^[\w-]+$", site):
            return False
        token = await connections.access_token("atlassian", uid)
        return bool(token) and await _get_ok(f"https://api.atlassian.com/ex/jira/{site}/rest/api/3/project/{key}", token, "application/json")

    return await _checked(("atlassian", uid, key), check)


async def blocked(provider: str, scope: str = "") -> list[str]:
    """Readers who cannot be shown to have access to what the asker's `provider` account just returned. `scope` is the repo or the Jira project key.
    A mailbox or a private chat is never shared. GitHub and Jira need the reader's own token to open the repo or project; with no scope to check, access is not verified, so they are blocked."""
    out = []
    for uid in _audience.get():
        if provider in PERSONAL or not scope:
            ok = False
        elif provider == "github":
            ok = await _can_read_repo(uid, scope)
        elif provider == "atlassian":
            ok = await _can_read_jira(uid, scope)
        else:
            ok = False
        if not ok:
            out.append(uid)
    return out


async def guard(provider: str, result: str, scope: str = "") -> str:
    """Pass a tool result through, noting when a reader may not see what it holds so the answer waits for the asker's say-so."""
    if _audience.get() and not result.startswith("Error:"):
        try:
            hold = bool(await blocked(provider, scope))
        except Exception:
            hold = True  # a failed check holds the answer back, never lets it through
        if hold:
            note(provider)
    return result
