"""core/audience.py: a bot's answer in a shared chat waits for the asker's say-so when the other reader cannot access its source."""
import asyncio
from types import SimpleNamespace

import pytest

from core import audience
from integrations import connections


@pytest.fixture
def reader(monkeypatch):
    audience._repo_ok.clear()
    monkeypatch.setattr(connections, "has", lambda provider, uid=None: False)
    tokens = audience.set_audience(["u2"])
    yield
    audience.reset_audience(tokens)


def run(coro):
    return asyncio.run(coro)


def repo_access(monkeypatch, status):
    async def token(provider, uid=None):
        return "tok"

    class Client:
        def __init__(self, **kw): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): pass
        async def get(self, url, headers=None): return SimpleNamespace(status_code=status)

    monkeypatch.setattr(connections, "access_token", token)
    monkeypatch.setattr(audience.httpx, "AsyncClient", Client)


def test_no_audience_records_nothing():
    assert run(audience.guard("gmail", "inbox")) == "inbox"
    assert audience.withheld() == []


def test_reader_who_cannot_open_the_repo_holds_the_answer_back(reader, monkeypatch):
    repo_access(monkeypatch, 404)  # GitHub answers 404 for a private repo the token cannot see
    assert run(audience.guard("github", "PR #4: fix login", "acme/app")) == "PR #4: fix login"  # the asker still gets it whole
    assert audience.withheld() == ["github"]


def test_reader_who_can_open_the_repo_sees_it_all(reader, monkeypatch):
    repo_access(monkeypatch, 200)
    run(audience.guard("github", "PR #4", "acme/app"))
    assert audience.withheld() == []


def test_mailbox_is_always_private_and_errors_are_ignored(reader):
    run(audience.guard("atlassian", "Error: nope"))
    assert audience.withheld() == []
    run(audience.guard("gmail", "3 emails"))
    assert audience.withheld() == ["gmail"]


def test_a_follow_up_inherits_the_last_restricted_answer(db):
    from core.models import DirectMessage, Project, User
    from routes.people import _carried

    db.add_all([Project(id="p", name="P"), User(id="a", email="a@x.y", name="A", password_hash="x"), User(id="b", email="b@x.y", name="B", password_hash="x")])
    db.flush()
    db.add(DirectMessage(project_id="p", sender_id="a", recipient_id="b", bot_id="bot", content="raw", withheld="github", share="shared"))
    db.add(DirectMessage(project_id="p", sender_id="a", recipient_id="b", content="what's in the repo again?"))
    db.commit()
    assert _carried(db, "p", "a", "b") == ["github"]
    assert _carried(db, "p", "a", "zzz") == []


def test_private_chats_are_never_open_to_a_reader(reader):
    assert run(audience.blocked("private chats")) == ["u2"]


def test_shared_chat_keeps_team_knowledge_but_names_no_one(db):
    from core.models import Agent, Message, Project, User, get_shared_context

    db.add_all([Project(id="p", name="P")] + [User(id=i, email=f"{i}@x.y", name=i.upper(), password_hash="x") for i in ("a", "b", "c")])
    db.flush()
    bot = Agent(id="bot", name="Bot", system_prompt="x", project_id="p")
    db.add(bot)
    db.add_all([Message(agent_id="bot", user_id="b", role="user", content="we ship friday"), Message(agent_id="bot", user_id="c", role="user", content="budget is 5k")])
    db.commit()
    from core.usage import reset_usage_user, set_usage_user
    token = set_usage_user("a", "p")
    try:
        assert "- B: we ship friday" in get_shared_context(bot)  # an ordinary chat names people, as before
        tokens = audience.set_audience(["b"])
        out = get_shared_context(bot)
        audience.reset_audience(tokens)
    finally:
        reset_usage_user(token)
    assert "budget is 5k" in out and "- A teammate: budget is 5k" in out and "C" not in out.replace("Lines naming", "")  # c's chat is still known, unnamed
    assert "- B: we ship friday" in out  # the reader's own chat keeps their name


# ── NURN-132: requester context and credential broker ──

def test_shared_chat_carries_a_requester_context():
    from core.usage import reset_usage_user, set_usage_user

    assert audience.requester() is None
    usage = set_usage_user("a", "p")
    tokens = audience.set_audience(["b"], "p:a:b")
    try:
        assert audience.requester() == audience.Requester("a", "p:a:b", ("a", "b"))
    finally:
        audience.reset_audience(tokens)
        reset_usage_user(usage)
    assert audience.requester() is None


def test_broker_fails_closed_naming_the_connector_and_only_in_shared_chats(monkeypatch, db):
    from core.models import User
    from core.usage import reset_usage_user, set_usage_user
    from tools.dispatch import run_tool

    db.add(User(id="a", email="a@x.y", name="Ann", password_hash="x"))
    db.commit()
    monkeypatch.setattr("core.database.SessionLocal", lambda: db)
    monkeypatch.setattr(db, "close", lambda: None)
    monkeypatch.setattr(connections, "_stored", lambda uid, provider: None)  # nobody has connected anything, as with an admin on env credentials
    usage = set_usage_user("a", "p")
    tokens = audience.set_audience(["b"], "p:a:b")
    try:
        for tool in ("github_get_file_contents", "gmail_search_messages", "getJiraIssue", "planSprint"):
            out = run(run_tool(tool, {}, "NURN", "acme/app", "bot"))
            assert out.startswith("Error:") and "not connected for Ann" in out and "server credential" in out, tool
        assert "GitHub is not connected" in run(run_tool("github_get_file_contents", {}, "NURN", "acme/app", "bot"))
        monkeypatch.setattr(connections, "_stored", lambda uid, provider: ({"access": "t"}, 0))
        run(connections.require("github"))  # connected: no error
    finally:
        audience.reset_audience(tokens)
        reset_usage_user(usage)
    monkeypatch.setattr(connections, "_stored", lambda uid, provider: None)
    run(connections.require("github"))  # outside a shared chat the legacy fallback is untouched


def test_an_answer_awaiting_approval_is_visible_to_the_asker_only(db):
    from core.models import DirectMessage, Project, ProjectMember, User
    from routes.people import thread

    a, b = (User(id=i, email=f"{i}@x.y", name=i.upper(), password_hash="x") for i in ("a", "b"))
    db.add_all([Project(id="p", name="P"), a, b])
    db.flush()
    db.add_all([ProjectMember(project_id="p", user_id="a", role="owner"), ProjectMember(project_id="p", user_id="b", role="member"),
                DirectMessage(project_id="p", sender_id="a", recipient_id="b", bot_id="bot", content="private answer", share="pending")])
    db.commit()
    project = db.get(Project, "p")
    assert [m["content"] for m in thread("b", a, project, db)["messages"]] == ["private answer"]
    assert thread("a", b, project, db)["messages"] == []


def test_broker_refuses_a_call_not_running_as_the_requester(monkeypatch):
    from core.usage import reset_usage_user, set_usage_user

    monkeypatch.setattr(connections, "_stored", lambda uid, provider: ({"access": "t"}, 0))
    usage = set_usage_user("a", "p")
    tokens = audience.set_audience(["b"], "p:a:b")
    other = set_usage_user("b", "p")  # identity drifts to the reader mid-turn
    try:
        with pytest.raises(connections.MissingToken, match="not running as the person who asked"):
            run(connections.require("github"))
    finally:
        reset_usage_user(other)
        audience.reset_audience(tokens)
        reset_usage_user(usage)
