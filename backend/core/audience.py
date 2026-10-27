"""Who else can read a bot's answer, and which of the asker's connected accounts they cannot see."""
import asyncio
import time
from contextvars import ContextVar, Token
from dataclasses import dataclass

import httpx

@dataclass(frozen=True)
class Requester:
    """Who a tool call in a shared chat acts for: the person who asked, in which chat, with whom."""
    user_id: str
    chat_id: str
    participants: tuple[str, ...]


_requester: ContextVar[Requester | None] = ContextVar("requester", default=None)
_audience: ContextVar[tuple[str, ...]] = ContextVar("audience", default=())
_withheld: ContextVar[set[str]] = ContextVar("withheld", default=set())  # the set is shared with child tasks, so tools running in them report back

def set_audience(user_ids: list[str], chat_id: str = "") -> tuple[Token, ...]:
    """The other people reading this turn's answer, and the requester context for its tool calls. Returns tokens for reset_audience."""
    from core.usage import current_user

    asker = current_user()
    return (_audience.set(tuple(user_ids)), _withheld.set(set()),
            _requester.set(Requester(asker, chat_id, (asker, *user_ids))))


def reset_audience(tokens: tuple[Token, ...]) -> None:
    _audience.reset(tokens[0])
    _withheld.reset(tokens[1])
    _requester.reset(tokens[2])


def requester() -> Requester | None:
    """The requester context of this tool call; None outside a shared chat."""
    return _requester.get()


def readers() -> tuple[str, ...]:
    """The other people reading this turn's answer; empty outside a shared chat."""
    return _audience.get()


def note(provider: str) -> None:
    """This turn used something the readers may not see, so the answer waits for the asker's say-so."""
    if _audience.get():
        _withheld.get().add(provider)


def withheld() -> list[str]:
    return sorted(_withheld.get())


PERSONAL = ("gmail", "private chats")  # never shared, whatever the reader has connected
_repo_ok: dict[tuple[str, str], tuple[float, bool]] = {}  # (user, repo) -> (expires, can read it)
_REPO_TTL = 300


async def _can_read_repo(uid: str, repo: str) -> bool:
    """True when this person's own GitHub token can open the repo. Fails closed on any error."""
    from integrations import connections

    hit = _repo_ok.get((uid, repo))
    if hit and hit[0] > time.time():
        return hit[1]
    ok = False
    try:
        token = await connections.access_token("github", uid)
        if not token and await asyncio.to_thread(connections.is_admin, uid):
            token = connections.env_fallback("github")
        if token:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.get(f"https://api.github.com/repos/{repo}", headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"})
            ok = resp.status_code == 200
    except Exception:
        pass  # unknown means no access
    _repo_ok[(uid, repo)] = (time.time() + _REPO_TTL, ok)
    return ok


async def blocked(provider: str, repo: str = "") -> list[str]:
    """Readers without access to what the asker's `provider` account just returned.
    A mailbox or a private chat is never shared. GitHub needs the reader's own token to open the project's repo. Atlassian needs the reader's own connection."""
    from integrations import connections

    out = []
    for uid in _audience.get():
        if provider in PERSONAL:
            ok = False
        elif provider == "github" and repo:
            ok = await _can_read_repo(uid, repo)
        else:
            ok = await asyncio.to_thread(connections.has, provider, uid)
        if not ok:
            out.append(uid)
    return out


async def guard(provider: str, result: str, repo: str = "") -> str:
    """Pass a tool result through, noting when a reader may not see what it holds so the answer waits for the asker's say-so."""
    if _audience.get() and not result.startswith("Error:") and await blocked(provider, repo):
        _withheld.get().add(provider)
    return result
