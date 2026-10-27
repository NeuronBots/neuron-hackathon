"""Access control in shared chats: who may read a bot's answer, the requester context, the credential broker, and the approval flow."""
import asyncio
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from core import audience
from core.models import Agent, DirectMessage, Message, Project, ProjectMember, User, get_shared_context
from core.usage import reset_usage_user, set_usage_user
from integrations import connections


def run(coro):
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def fresh_caches():
    connections._cache.clear()  # a token looked up in one test must not answer for the next
    audience._access.clear()
    yield
    connections._cache.clear()
    audience._access.clear()


@pytest.fixture
def reader(monkeypatch):
    """A turn asked by "a" and read by "u2", who has nothing connected."""
    audience._access.clear()
    monkeypatch.setattr(connections, "_stored", lambda uid, provider: None)
    usage = set_usage_user("a", "p")
    tokens = audience.set_audience(["u2"], "p:a:u2")
    yield
    audience.reset_audience(tokens)
    reset_usage_user(usage)


def http(monkeypatch, status=200, seen=None):
    """Every request the access checks make answers `status`; `seen` collects (url, token)."""
    async def get_ok(url, token, accept):
        if seen is not None:
            seen.append((url, token))
        return status == 200

    monkeypatch.setattr(audience, "_get_ok", get_ok)


def token_for(monkeypatch, token):
    async def access_token(provider, uid=None):
        return token

    monkeypatch.setattr(connections, "access_token", access_token)


def project_cloud(db, cloud="site-1"):
    db.add(Project(id="p", name="P", jira_project_key="NURN", jira_cloud_id=cloud))
    db.commit()


# ── what the readers may see ──

def test_no_audience_records_nothing():
    assert run(audience.guard("gmail", "inbox")) == "inbox"
    audience.note("gmail")  # no turn is running: nothing to add to, and no shared default to leak into the next one
    assert audience.withheld() == []


def test_reader_who_cannot_open_the_repo_holds_the_answer_back(reader, monkeypatch):
    token_for(monkeypatch, "tok")
    http(monkeypatch, status=404)  # GitHub answers 404 for a private repo the token cannot see
    assert run(audience.guard("github", "PR #4: fix login", "acme/app")) == "PR #4: fix login"  # the asker still gets it whole
    assert audience.withheld() == ["github"]


def test_reader_who_can_open_the_repo_sees_it_all(reader, monkeypatch):
    token_for(monkeypatch, "tok")
    http(monkeypatch)
    run(audience.guard("github", "PR #4", "acme/app"))
    assert audience.withheld() == []


def test_a_reader_without_a_token_of_their_own_is_blocked_even_when_an_env_token_exists(reader, monkeypatch):
    seen = []
    token_for(monkeypatch, None)
    http(monkeypatch, seen=seen)
    monkeypatch.setattr(connections, "is_admin", lambda uid: True)  # an admin
    monkeypatch.setattr(connections, "env_fallback", lambda provider: "server-token")
    run(audience.guard("github", "PR #4", "acme/app"))
    assert audience.withheld() == ["github"] and seen == []  # the server token was never tried


def test_no_repo_or_a_malformed_one_is_never_verified(reader, monkeypatch):
    seen = []
    token_for(monkeypatch, "tok")
    http(monkeypatch, seen=seen)
    for scope in ("", "x/../../user", "no-slash", "a/b/c"):
        audience._access.clear()
        assert run(audience.blocked("github", scope)) == ["u2"], scope
    assert seen == []


def test_the_repo_path_is_quoted_in_the_request(reader, monkeypatch):
    seen = []
    token_for(monkeypatch, "tok")
    http(monkeypatch, seen=seen)
    run(audience.blocked("github", "acme/app.js"))
    assert seen == [("https://api.github.com/repos/acme/app.js", "tok")]


def test_jira_needs_the_readers_access_to_the_project(reader, db, monkeypatch):
    project_cloud(db)
    seen = []
    token_for(monkeypatch, "tok")
    http(monkeypatch, status=403, seen=seen)
    assert run(audience.blocked("atlassian", "NURN")) == ["u2"]
    assert seen == [("https://api.atlassian.com/ex/jira/site-1/rest/api/3/project/NURN", "tok")]
    audience._access.clear()
    http(monkeypatch)
    assert run(audience.blocked("atlassian", "NURN")) == []


def test_jira_with_no_project_key_or_site_is_not_verified(reader, db, monkeypatch):
    project_cloud(db, cloud="")
    token_for(monkeypatch, "tok")
    http(monkeypatch)
    assert run(audience.blocked("atlassian", "NURN")) == ["u2"]  # no site to check against
    assert run(audience.blocked("atlassian", "")) == ["u2"]  # Confluence and Compass have no project


def test_mailbox_and_private_chats_are_never_open_to_a_reader(reader):
    assert run(audience.blocked("gmail")) == ["u2"]
    assert run(audience.blocked("private chats")) == ["u2"]


def test_errors_are_not_held_and_a_failed_check_holds_the_answer(reader, monkeypatch):
    run(audience.guard("atlassian", "Error: nope", "NURN"))
    assert audience.withheld() == []

    async def boom(provider, scope=""):
        raise RuntimeError("db down")

    monkeypatch.setattr(audience, "blocked", boom)
    run(audience.guard("github", "PR #4", "acme/app"))
    assert audience.withheld() == ["github"]


def test_access_results_are_cached_briefly_and_the_cache_is_bounded(reader, monkeypatch):
    calls = []
    token_for(monkeypatch, "tok")

    async def get_ok(url, token, accept):
        calls.append(url)
        return True

    monkeypatch.setattr(audience, "_get_ok", get_ok)
    run(audience.blocked("github", "acme/app"))
    run(audience.blocked("github", "acme/app"))
    assert len(calls) == 1
    for n in range(audience._ACCESS_MAX + 20):
        run(audience.blocked("github", f"acme/app{n}"))
    assert len(audience._access) <= audience._ACCESS_MAX


def test_scrub_takes_out_links_emails_and_ids_and_caps_the_length():
    out = audience.scrub("See https://x.io/a?b=1 or mail ann@corp.com about NURN-132 at 0123abcd4567 now")
    assert out == "See [link] or mail [email] about [id] at [id] now"
    assert len(audience.scrub("word " * 400, 50)) <= 51


# ── NURN-132: requester context and credential broker ──

def test_shared_chat_carries_a_requester_context():
    assert audience.requester() is None
    usage = set_usage_user("a", "p")
    tokens = audience.set_audience(["b"], "p:a:b")
    try:
        assert audience.requester() == audience.Requester("a", "p:a:b", ("a", "b"))
    finally:
        audience.reset_audience(tokens)
        reset_usage_user(usage)
    assert audience.requester() is None


def test_broker_fails_closed_naming_the_connector_and_only_in_shared_chats(reader, db):
    from tools.dispatch import run_tool

    db.add(User(id="a", email="a@x.y", name="Ann", password_hash="x"))
    db.commit()
    for tool in ("github_get_file_contents", "gmail_search_messages", "getJiraIssue", "planSprint"):
        out = run(run_tool(tool, {}, "NURN", "acme/app", "bot"))
        assert out.startswith("Error:") and "not connected for Ann" in out and "server credential" in out, tool
    assert "GitHub is not connected" in run(run_tool("github_get_file_contents", {}, "NURN", "acme/app", "bot"))


def test_broker_lets_a_connected_requester_through_and_leaves_legacy_chats_alone(monkeypatch):
    monkeypatch.setattr(connections, "_stored", lambda uid, provider: ({"access": "t"}, 0))
    usage = set_usage_user("a", "p")
    tokens = audience.set_audience(["b"], "p:a:b")
    try:
        run(connections.require("github"))
    finally:
        audience.reset_audience(tokens)
    monkeypatch.setattr(connections, "_stored", lambda uid, provider: None)
    run(connections.require("github"))  # outside a shared chat the legacy fallback is untouched
    reset_usage_user(usage)


def test_broker_refuses_a_token_that_will_not_refresh(db, monkeypatch):
    db.add(User(id="a", email="a@x.y", name="Ann", password_hash="x"))
    db.commit()
    monkeypatch.setattr(connections, "_stored", lambda uid, provider: ({"access": "old", "refresh": "r"}, 1))  # expired long ago

    async def refresh_fails(provider, body):
        raise RuntimeError("invalid_grant")

    monkeypatch.setattr(connections, "_token_request", refresh_fails)
    usage = set_usage_user("a", "p")
    tokens = audience.set_audience(["b"], "p:a:b")
    try:
        with pytest.raises(connections.MissingToken, match="not connected for Ann"):
            run(connections.require("github"))
    finally:
        audience.reset_audience(tokens)
        reset_usage_user(usage)


def test_broker_refuses_a_call_not_running_as_the_requester(monkeypatch):
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


def test_the_tool_clients_refuse_their_env_fallbacks_in_a_shared_chat(monkeypatch):
    from integrations import atlassian_mcp, github_mcp

    token_for(monkeypatch, None)
    monkeypatch.setattr(github_mcp, "GITHUB_PAT", "server-pat")
    monkeypatch.setattr(atlassian_mcp, "_auth_header", lambda: "Basic server")
    monkeypatch.setattr(atlassian_mcp, "is_enabled", lambda: True)
    assert run(github_mcp._headers())["Authorization"] == "Bearer server-pat"  # legacy path
    assert run(atlassian_mcp._headers()) == {"Authorization": "Basic server"}
    usage = set_usage_user("a", "p")
    tokens = audience.set_audience(["b"], "p:a:b")
    try:
        with pytest.raises(RuntimeError, match="GitHub is not connected"):
            run(github_mcp._headers())
        assert run(atlassian_mcp._headers()) == {}
        with pytest.raises(RuntimeError, match="Atlassian is not connected"):
            run(atlassian_mcp.call_tool("getJiraIssue", {}))
    finally:
        audience.reset_audience(tokens)
        reset_usage_user(usage)


def test_a_name_no_integration_owns_is_not_blamed_on_atlassian(reader):
    from tools.dispatch import run_tool

    assert run(run_tool("no_such_tool", {}, "NURN", "acme/app", "bot")) == "Error: unknown tool no_such_tool."


# ── the approval flow ──

@pytest.fixture
def chat(db):
    """Ann (a) asked a bot, Ben (b) reads; one answer waits for Ann, one ordinary message each way."""
    db.add_all([Project(id="p", name="P")] + [User(id=i, email=f"{i}@x.y", name=i.upper(), password_hash="x") for i in ("a", "b")])
    db.flush()
    db.add_all([ProjectMember(project_id="p", user_id="a", role="owner"), ProjectMember(project_id="p", user_id="b", role="member")])
    db.commit()
    users = {i: db.get(User, i) for i in ("a", "b")}

    def add(content, share="", sender="a", recipient="b", bot="", withheld="", reply_to=None, action=""):
        row = DirectMessage(project_id="p", sender_id=sender, recipient_id=recipient, bot_id=bot, content=content, share=share, withheld=withheld, reply_to=reply_to, action=action)
        db.add(row)
        db.commit()
        return row

    return SimpleNamespace(db=db, project=db.get(Project, "p"), a=users["a"], b=users["b"], add=add)


def test_an_answer_awaiting_approval_is_visible_to_the_asker_only(chat):
    from routes.people import thread

    chat.add("private answer", share="pending", bot="bot")
    assert [m["content"] for m in thread("b", chat.a, chat.project, chat.db)["messages"]] == ["private answer"]
    assert thread("a", chat.b, chat.project, chat.db)["messages"] == []


def test_the_other_person_cannot_fetch_react_to_or_reply_to_a_held_answer_by_id(chat):
    from routes.people import DirectIn, ReactIn, react, send

    for state in ("pending", "sharing", "declined", "shared"):
        held = chat.add("private answer", share=state, bot="bot")
        with pytest.raises(HTTPException) as err:
            react("a", held.id, ReactIn(emoji="👍"), chat.b, chat.project, chat.db)
        assert err.value.status_code == 404, state
        with pytest.raises(HTTPException) as err:
            run(send("a", DirectIn(content="hi", reply_to=held.id), chat.b, chat.project, chat.db))
        assert err.value.status_code == 400, state
        assert react("b", held.id, ReactIn(emoji="👍"), chat.a, chat.project, chat.db)["content"] == "private answer"  # the asker still can


def test_held_answers_do_not_reach_the_other_persons_latest_message_or_unread_count(chat):
    from routes.people import SeenIn, latest_per_person, unread_counts

    chat.add("hello", sender="a")
    chat.add("private answer", share="pending", bot="bot")
    assert latest_per_person(chat.b, chat.project, chat.db)["a"]["content"] == "hello"
    assert unread_counts(SeenIn(people={"a": 0}), chat.b, chat.project, chat.db)["people"] == {"a": 1}
    assert latest_per_person(chat.a, chat.project, chat.db)["b"]["content"] == "private answer"


def test_only_the_asker_decides_and_only_once(chat):
    from routes.people import ShareIn, decide_share

    held = chat.add("private answer", share="pending", bot="bot")
    with pytest.raises(HTTPException) as err:
        run(decide_share("a", held.id, ShareIn(share=False), chat.b, chat.project, chat.db))
    assert err.value.status_code == 404  # the other person never even learns it exists
    out = run(decide_share("b", held.id, ShareIn(share=False), chat.a, chat.project, chat.db))
    assert out["share"] == "declined"
    with pytest.raises(HTTPException) as err:
        run(decide_share("b", held.id, ShareIn(share=True), chat.a, chat.project, chat.db))
    assert err.value.status_code == 409
    refusals = chat.db.query(DirectMessage).filter(DirectMessage.content == "Sorry, you don't have access to this.").all()
    assert len(refusals) == 1 and refusals[0].share == ""  # the other person is told, once


def test_yes_starts_one_summary_and_a_second_click_cannot_start_another(chat, monkeypatch):
    from routes import people
    from routes.people import ShareIn, decide_share

    started = []
    monkeypatch.setattr(people.runs, "spawn", lambda coro: (started.append(coro), coro.close()))
    held = chat.add("private answer", share="pending", bot="bot")
    assert run(decide_share("b", held.id, ShareIn(share=True), chat.a, chat.project, chat.db))["share"] == "sharing"
    with pytest.raises(HTTPException) as err:
        run(decide_share("b", held.id, ShareIn(share=True), chat.a, chat.project, chat.db))
    assert err.value.status_code == 409 and len(started) == 1


def test_the_summary_is_scrubbed_and_the_answer_reads_shared_only_once_it_is_posted(chat, monkeypatch):
    from routes.people import _post_summary

    monkeypatch.setattr("llm.client.complete", lambda model, system, user, max_tokens: "It covers the login fix, see https://x.io/pr/4, mail ann@corp.com.")
    held = chat.add("private answer", share="sharing", bot="bot", withheld="github")
    run(_post_summary(held.id))
    chat.db.expire_all()
    posted = chat.db.query(DirectMessage).filter(DirectMessage.id != held.id).one()
    assert posted.content == "It covers the login fix, see [link], mail [email]." and posted.share == "" and posted.bot_id == "bot"
    assert chat.db.get(DirectMessage, held.id).share == "shared"


def test_a_summary_that_fails_goes_back_to_pending_and_tells_the_other_person_nothing(chat, monkeypatch):
    from routes.people import _post_summary

    def fails(model, system, user, max_tokens):
        raise RuntimeError("upstream 500")

    monkeypatch.setattr("llm.client.complete", fails)
    held = chat.add("private answer", share="sharing", bot="bot", withheld="github")
    run(_post_summary(held.id))
    chat.db.expire_all()
    assert chat.db.get(DirectMessage, held.id).share == "pending"  # the asker can try again
    assert chat.db.query(DirectMessage).count() == 1


def test_a_follow_up_inherits_the_last_restricted_answer_in_its_thread_only(chat):
    from routes.people import _carried

    first = chat.add("raw", share="shared", bot="bot", withheld="github")
    chat.add("raw in a thread", share="shared", bot="bot", withheld="atlassian", reply_to=first.id)
    chat.add("what's in the repo again?")
    assert _carried(chat.db, "p", "a", "b") == ["github"]  # the main chat
    assert _carried(chat.db, "p", "a", "b", first.id) == ["atlassian", "github"]
    assert _carried(chat.db, "p", "a", "zzz") == []


def test_a_declined_answer_and_a_private_chat_do_not_keep_a_thread_restricted(chat):
    from routes.people import _carried

    chat.add("raw", share="declined", bot="bot", withheld="github")
    chat.add("raw", share="shared", bot="bot", withheld="private chats")
    assert _carried(chat.db, "p", "a", "b") == []


# ── bots keep the team's knowledge, but a shared reply carries no names ──

def test_shared_chat_keeps_team_knowledge_but_names_no_one_and_scrubs_it(db):
    db.add_all([Project(id="p", name="P")] + [User(id=i, email=f"{i}@x.y", name=n, password_hash="x") for i, n in (("a", "Alice"), ("b", "Bruno"), ("c", "Carol"))])
    db.flush()
    bot = Agent(id="bot", name="Bot", system_prompt="x", project_id="p")
    db.add(bot)
    db.add_all([Message(agent_id="bot", user_id="b", role="user", content="we ship friday"),
                Message(agent_id="bot", user_id="c", role="user", content="budget is 5k, ask carol@corp.com or see https://wiki.io/x")])
    db.commit()
    usage = set_usage_user("a", "p")
    try:
        plain = get_shared_context(bot)
        assert "- Bruno: we ship friday" in plain and "- Carol: budget is 5k" in plain  # an ordinary chat names people, as before
        tokens = audience.set_audience(["b"], "p:a:b")
        shared = get_shared_context(bot)
        audience.reset_audience(tokens)
    finally:
        reset_usage_user(usage)
    assert "- A teammate: budget is 5k, ask [email] or see [link]" in shared  # c's chat is still known, unnamed and scrubbed
    assert "Carol" not in shared and "carol@corp.com" not in shared and "wiki.io" not in shared
    assert "- Bruno: we ship friday" in shared  # the reader's own chat keeps their name


# ── writes: held for the asker's Proceed, then a one-line note for the other person ──

def test_reads_and_writes_are_told_apart():
    from tools.writes import is_write

    for name in ("getJiraIssue", "searchJiraIssuesUsingJql", "lookupJiraAccountId", "atlassianUserInfo", "listJiraBoardSprints", "github_get_file_contents",
                 "github_list_branches", "github_search_code", "gmail_search_messages", "gmail_read_message", "gmail_list_labels", "executeRead"):
        assert not is_write(name), name
    for name in ("createJiraIssue", "editJiraIssue", "transitionJiraIssue", "addOrEditJiraIssueComment", "planSprint", "manageJiraSprint",
                 "github_create_issue", "github_create_pull_request", "github_merge_pull_request", "gmail_send_message", "gmail_create_draft", "somethingNew"):
        assert is_write(name), name


def test_the_other_person_is_told_that_it_was_done_not_what_it_held():
    from tools.writes import done_note

    made = done_note("createJiraIssue", {"summary": "Add login, mail ann@corp.com", "issueTypeName": "Story"}, '{"key": "NURN-141", "id": "9"}')
    assert made == "Story “Add login, mail [email]” has been created in Jira (NURN-141)."
    assert done_note("transitionJiraIssue", {"issueIdOrKey": "NURN-9"}, "ok") == "NURN-9 has been moved to a new status in Jira."
    assert done_note("gmail_send_message", {"to": "x@y.z", "body": "secret"}, "ok") == "An email has been sent."
    assert done_note("github_create_issue", {"title": "Bug"}, "ok") == "GitHub issue “Bug” has been opened."


def test_a_write_in_a_shared_chat_is_held_not_run_and_a_read_is_not(monkeypatch, db):
    from tools import dispatch

    db.add(User(id="a", email="a@x.y", name="Ann", password_hash="x"))
    db.commit()
    monkeypatch.setattr(connections, "_stored", lambda uid, provider: ({"access": "t"}, 0))
    ran = []

    async def call(name, args):
        ran.append(name)
        return '{"key": "NURN-1"}'

    monkeypatch.setattr(dispatch, "call_atlassian_tool", call)
    monkeypatch.setattr(dispatch, "atlassian_owns", lambda name: True)
    usage = set_usage_user("a", "p")
    tokens = audience.set_audience(["b"], "p:a:b")
    try:
        out = run(dispatch.run_tool("createJiraIssue", {"projectKey": "NURN", "summary": "Add login"}, "NURN", "", "bot"))
        assert "NOT done yet" in out and "Proceed" in out and ran == []
        run(dispatch.run_tool("createJiraIssue", {"projectKey": "NURN", "summary": "Add login"}, "NURN", "", "bot"))  # the same call twice is one card
        assert [c["tool"] for c in audience.actions()] == ["createJiraIssue"]
        run(dispatch.run_tool("getJiraIssue", {"issueIdOrKey": "NURN-1"}, "NURN", "", "bot"))
        assert ran == ["getJiraIssue"]
    finally:
        audience.reset_audience(tokens)
    tokens = audience.set_audience(["b"], "p:a:b", approved=True)  # the asker pressed Proceed: it runs
    try:
        run(dispatch.run_tool("createJiraIssue", {"projectKey": "NURN", "summary": "Add login"}, "NURN", "", "bot"))
        assert ran == ["getJiraIssue", "createJiraIssue"]
    finally:
        audience.reset_audience(tokens)
        reset_usage_user(usage)


def test_a_turn_that_prepares_a_write_shows_the_asker_a_card_and_the_other_person_nothing(chat, monkeypatch):
    from routes import chat as chat_routes
    from routes.people import _bot_answers, thread
    from tools import dispatch

    chat.db.add(Agent(id="bot", name="Bot", system_prompt="x", project_id="p"))
    chat.db.commit()
    monkeypatch.setattr(connections, "_stored", lambda uid, provider: ({"access": "t"}, 0))
    monkeypatch.setattr(dispatch, "atlassian_owns", lambda name: True)

    async def stream(bot_id, body, user, project, db):
        out = await dispatch.run_tool("createJiraIssue", {"projectKey": "NURN", "summary": "Add login"}, "NURN", "", bot_id)
        assert "NOT done yet" in out
        yield 'data: {"type": "done", "reply": "I prepared the story."}\n\n'

    monkeypatch.setattr(chat_routes, "_chat_stream", stream)
    run(_bot_answers("p", "a", "b", "bot", "@Bot create a story", None))
    chat.db.expire_all()
    mine = thread("b", chat.a, chat.project, chat.db)["messages"]
    assert [(m["share"], m["action"]) for m in mine] == [("private", False), ("proceed", True)]
    assert "Add login" in mine[1]["content"]
    assert thread("a", chat.b, chat.project, chat.db)["messages"] == []


def test_cancel_tells_no_one_and_only_the_asker_can_decide_once(chat):
    from routes.people import ProceedIn, decide_action

    card = chat.add("Create a story", share="proceed", bot="bot", action='{"provider": "atlassian"}')
    with pytest.raises(HTTPException) as err:
        run(decide_action("a", card.id, ProceedIn(proceed=True), chat.b, chat.project, chat.db))
    assert err.value.status_code == 404
    assert run(decide_action("b", card.id, ProceedIn(proceed=False), chat.a, chat.project, chat.db))["share"] == "cancelled"
    with pytest.raises(HTTPException) as err:
        run(decide_action("b", card.id, ProceedIn(proceed=True), chat.a, chat.project, chat.db))
    assert err.value.status_code == 409
    assert chat.db.query(DirectMessage).count() == 1


def test_proceed_runs_the_write_as_the_asker_then_tells_the_other_person_once(chat, monkeypatch):
    from routes import people
    from routes.people import ProceedIn, _run_action, decide_action, thread
    from tools import dispatch

    seen = []

    async def fake_run_tool(name, args, jira_key="", repo="", agent_id=""):
        seen.append((name, args, audience.requester(), audience.hold_write("atlassian", name, args, jira_key, repo, agent_id)))
        return '{"key": "NURN-141"}'

    monkeypatch.setattr(dispatch, "run_tool", fake_run_tool)
    started = []
    monkeypatch.setattr(people.runs, "spawn", lambda coro: (started.append(coro), coro.close()))
    call = {"provider": "atlassian", "tool": "createJiraIssue", "args": {"summary": "Add login", "issueTypeName": "Story"}, "jira_key": "NURN", "repo": "", "agent_id": "bot"}
    import json
    card = chat.add("Create a story", share="proceed", bot="bot", action=json.dumps(call))
    assert run(decide_action("b", card.id, ProceedIn(proceed=True), chat.a, chat.project, chat.db))["share"] == "running" and len(started) == 1
    run(_run_action(card.id))
    chat.db.expire_all()
    name, args, who, held = seen[0]
    assert name == "createJiraIssue" and who.user_id == "a" and held == ""  # as the asker, and not held again
    assert chat.db.get(DirectMessage, card.id).share == "done"
    told = [m["content"] for m in thread("a", chat.b, chat.project, chat.db)["messages"]]
    assert told == ["Story “Add login” has been created in Jira (NURN-141)."]


def test_a_write_that_fails_goes_back_to_proceed_and_only_the_asker_hears_why(chat, monkeypatch):
    import json

    from routes.people import _run_action, thread
    from tools import dispatch

    async def fails(name, args, jira_key="", repo="", agent_id=""):
        return "Error: Jira said no for ann@corp.com"

    monkeypatch.setattr(dispatch, "run_tool", fails)
    call = {"provider": "atlassian", "tool": "createJiraIssue", "args": {"summary": "x"}, "jira_key": "NURN", "repo": "", "agent_id": "bot"}
    card = chat.add("Create a story", share="running", bot="bot", action=json.dumps(call))
    run(_run_action(card.id))
    chat.db.expire_all()
    assert chat.db.get(DirectMessage, card.id).share == "proceed"
    assert thread("a", chat.b, chat.project, chat.db)["messages"] == []
    mine = [m["content"] for m in thread("b", chat.a, chat.project, chat.db)["messages"]]
    assert len(mine) == 2 and "Jira said no for [email]" in mine[1]
