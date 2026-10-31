"""A bot working on its own may not act outside the app (Jira, mail, GitHub) until a person approves."""
import asyncio
import json
from datetime import datetime

import pytest

from core.access import hash_password
from core.models import Agent, Message, Project, ProjectMember, Task, TaskEvent, User
from core.usage import reset_usage_user, set_usage_user
from tools import outbox
from tools.outbox import MAIL_DRAFT, is_outward, queue_outward

JIRA_WRITES = {"createJiraIssue", "addCommentToJiraIssue", "getJiraIssue"}


@pytest.fixture
def world(db, monkeypatch):
    monkeypatch.setattr("integrations.atlassian_mcp.owns", lambda name: name in JIRA_WRITES)
    user = User(id="user_appr", email="appr@example.com", name="Appr", password_hash=hash_password("testpass123"), plan="free")
    proj = Project(id="proj_appr", name="Approvals")
    db.add_all([user, proj])
    db.commit()
    db.add(ProjectMember(project_id=proj.id, user_id=user.id, role="owner"))
    bot = Agent(id="agent_appr", name="Worker", system_prompt="x", project_id=proj.id)
    db.add(bot)
    db.commit()
    token = set_usage_user(user.id, proj.id)
    yield user, proj, bot
    reset_usage_user(token)


def _events(db, task):
    return [e.text for e in db.query(TaskEvent).filter(TaskEvent.task_id == task.id).order_by(TaskEvent.id).all()]


def _task(db, bot):
    return db.query(Task).filter(Task.agent_id == bot.id).order_by(Task.created_at.desc()).first()


@pytest.mark.parametrize("name,outward", [
    ("addCommentToJiraIssue", True), ("createJiraIssue", True), ("manageJiraSprint", True), ("attachDocumentToJiraIssue", True),
    ("gmail_send_message", True), ("gmail_create_draft", True), ("github_create_issue", True), ("github_push_files", True),
    ("getJiraIssue", False), ("gmail_search_messages", False), ("github_get_file_contents", False),
    ("create_document", False), ("web_search", False), ("add_board_task", False), ("some_unknown_tool", False),
])
def test_only_writes_through_a_connected_account_are_outward(world, name, outward):
    assert is_outward(name) is outward


def test_a_jira_comment_waits_for_approval_and_nothing_runs(db, world):
    user, proj, bot = world
    args = {"issueIdOrKey": "ABC-1", "commentBody": "Shipping today"}
    text, event = queue_outward(bot.id, "addCommentToJiraIssue", args, "ABC", "", db)

    assert text.startswith("NOT done yet") and "do not call it again" in text
    task = _task(db, bot)
    assert (task.status, event) == ("decision", {"type": "task_decision", "title": task.title})
    assert task.title.startswith("Approve: Comment on ABC-1 in Jira")
    events = _events(db, task)
    assert "Shipping today" in events[0] and events[0].startswith("Needs your permission")
    call = json.loads(events[-1][len(MAIL_DRAFT):])
    assert call == {"tool": "addCommentToJiraIssue", "args": args, "jira_key": "ABC", "repo": ""}  # the scope is kept for the approved run

    (card_row,) = db.query(Message).filter(Message.agent_id == bot.id).all()  # the same decision, as a card in the bot's chat
    assert card_row.role == "assistant" and card_row.content.startswith("[[SOURCES:")
    card = json.loads(card_row.content[len("[[SOURCES:"):-2])
    assert (card["kind"], card["task_id"]) == ("approval", task.id) and "Shipping today" in card["detail"]


def test_mail_and_pushes_keep_their_own_drafts_and_a_read_just_runs(db, world):
    user, proj, bot = world
    mail, _ = queue_outward(bot.id, "gmail_send_message", {"to": "a@b.co", "subject": "Hi", "body": "x"}, "", "", db)
    assert mail.startswith("NOT sent") and _task(db, bot).title == "Send email: Hi"
    push, _ = queue_outward(bot.id, "github_create_or_update_file", {"owner": "o", "repo": "r", "path": "a.txt", "content": "x", "message": "m"}, "", "o/r", db)
    assert push.startswith("NOT pushed")
    assert queue_outward(bot.id, "getJiraIssue", {"issueIdOrKey": "ABC-1"}, "ABC", "", db) is None
    assert queue_outward(bot.id, "create_document", {"title": "t"}, "", "", db) is None


def test_a_full_queue_tells_the_bot_and_parks_nothing(db, world, monkeypatch):
    user, proj, bot = world
    monkeypatch.setattr(outbox, "decision_cap_msg", lambda agent_id, db: "NOT queued: Decisions pending is full")
    text, event = queue_outward(bot.id, "createJiraIssue", {"summary": "s"}, "ABC", "", db)
    assert text.startswith("NOT queued") and event is None and db.query(Task).count() == 0


def _decide(db, user, proj, task, answer, monkeypatch):
    from routes import tasks
    ran, kicked = [], []

    async def fake_run_tool(*a):
        ran.append(a)
        return "ok: done"
    monkeypatch.setattr(tasks, "run_tool", fake_run_tool)
    monkeypatch.setattr("routes.chat._start_kickoff", lambda *a, **k: kicked.append(a))
    asyncio.run(tasks._apply_decision(task, answer, user, proj, db))
    return ran, kicked


def test_approving_runs_the_stored_call_with_its_original_scope(db, world, monkeypatch):
    user, proj, bot = world
    args = {"projectKey": "ABC", "summary": "New story"}
    queue_outward(bot.id, "createJiraIssue", args, "ABC", "", db)
    task = _task(db, bot)

    ran, kicked = _decide(db, user, proj, task, "Approved", monkeypatch)

    assert ran == [("createJiraIssue", args, "ABC", "", bot.id)]
    assert task.status == "working" and any(t.startswith("Done: ok: done") for t in _events(db, task)) and len(kicked) == 1
    assert "approved and done" in kicked[0][3]  # the bot is told it must not do it again


def test_declining_runs_nothing_and_the_bot_hears_it(db, world, monkeypatch):
    user, proj, bot = world
    queue_outward(bot.id, "addCommentToJiraIssue", {"issueIdOrKey": "ABC-1", "commentBody": "x"}, "ABC", "", db)
    task = _task(db, bot)

    ran, kicked = _decide(db, user, proj, task, "Declined", monkeypatch)

    assert ran == [] and "NOT done" in kicked[0][3]


def test_a_card_can_read_its_task_back(db, world):
    from routes.tasks import get_task
    user, proj, bot = world
    queue_outward(bot.id, "createJiraIssue", {"summary": "s"}, "ABC", "", db)
    task = _task(db, bot)
    read = get_task(task.id, project=proj, db=db)
    assert (read["id"], read["status"]) == (task.id, "decision") and read["decisions"]
    with pytest.raises(Exception):
        get_task("nope", project=proj, db=db)


@pytest.mark.parametrize("path,condition", [
    ("routes/chat.py", "elif gated and (held := queue_outward("),
    ("agents/peer_run.py", "elif (held := queue_outward("),  # a teammate working for another bot always is
])
def test_every_autonomous_path_asks_the_gate_before_running_a_tool(path, condition):
    """Not an end-to-end run (no harness drives a model turn here): it keeps either path from losing its gate, weakening the
    condition, or running a tool ahead of it."""
    from pathlib import Path
    src = (Path(__file__).resolve().parents[1] / path).read_text()
    assert src.count(condition) == 1, f"{path} no longer gates outward writes with {condition!r}"
    assert src.index(condition) < src.index("await run_tool("), f"{path} runs a tool before the gate"
    assert src.count("await run_tool(") == 1, f"{path} has a second way to run a tool that skips the gate"


def test_chat_gates_autonomous_turns_and_a_persons_own_chat_until_they_allow_it():
    """The condition itself: a kickoff always; a person's chat unless they allowed it this session; a shared chat never (it has its own hold)."""
    from pathlib import Path
    src = (Path(__file__).resolve().parents[1] / "routes/chat.py").read_text()
    assert src.count("gated = kickoff or (not body.outward_ok and audience.requester() is None)") == 1


def test_a_person_in_the_chat_gets_a_card_with_three_choices_and_no_push(db, world, monkeypatch):
    user, proj, bot = world
    pushed = []
    monkeypatch.setattr(outbox, "notify_pending", lambda *a: pushed.append(a))
    monkeypatch.setattr(outbox, "decision_cap_msg", lambda agent_id, db: "NOT queued: Decisions pending is full")  # the board's limit is for bots alone

    text, _ = queue_outward(bot.id, "addCommentToJiraIssue", {"issueIdOrKey": "ABC-1", "commentBody": "hi"}, "ABC", "", db, interactive=True)

    assert "a card in this chat asks them to Allow or Deny" in text and "NOT done" in text
    assert pushed == []
    (card_row,) = db.query(Message).filter(Message.agent_id == bot.id).all()
    card = json.loads(card_row.content[len("[[SOURCES:"):-2])
    assert (card["interactive"], card["agent_id"]) == (True, bot.id)
    assert _task(db, bot).status == "decision"


def test_a_bot_on_its_own_still_pushes_a_notification_and_obeys_the_board_limit(db, world, monkeypatch):
    user, proj, bot = world
    pushed = []
    monkeypatch.setattr(outbox, "notify_pending", lambda *a: pushed.append(a))
    queue_outward(bot.id, "createJiraIssue", {"summary": "s"}, "ABC", "", db)
    assert len(pushed) == 1
    (card_row,) = db.query(Message).filter(Message.agent_id == bot.id).all()
    assert json.loads(card_row.content[len("[[SOURCES:"):-2])["interactive"] is False


@pytest.mark.parametrize("name", ["gmail_send_message", "github_push_files"])
def test_mail_and_pushes_are_asked_in_the_chat_too(db, world, name):
    user, proj, bot = world
    args = {"to": "a@b.co", "subject": "Hi", "body": "x"} if name.startswith("gmail") else {"owner": "o", "repo": "r", "branch": "b", "files": [{"path": "a", "content": "x"}], "message": "m"}
    text, _ = queue_outward(bot.id, name, args, "", "o/r", db, interactive=True)
    assert "a card in this chat" in text and _task(db, bot).status == "decision"


def test_allow_in_this_session_runs_the_action_like_allow(db, world, monkeypatch):
    user, proj, bot = world
    args = {"issueIdOrKey": "ABC-1", "commentBody": "hi"}
    queue_outward(bot.id, "addCommentToJiraIssue", args, "ABC", "", db, interactive=True)
    ran, kicked = _decide(db, user, proj, _task(db, bot), "Approved for this session", monkeypatch)
    assert ran == [("addCommentToJiraIssue", args, "ABC", "", bot.id)] and len(kicked) == 1


def test_a_chat_request_does_not_allow_outward_writes_unless_it_says_so():
    from core.schemas import ChatRequest
    assert ChatRequest(content="hi").outward_ok is False
    assert ChatRequest(content="hi", outward_ok=True).outward_ok is True


# ── Long calls survive approval; several requests in one turn are asked once ──

def _card_of(db, bot):
    rows = db.query(Message).filter(Message.agent_id == bot.id, Message.role == "assistant").order_by(Message.id).all()
    return [json.loads(r.content[len("[[SOURCES:"):-2]) for r in rows if r.content.startswith("[[SOURCES:")]


def test_a_long_call_is_stored_whole_and_runs_exactly_on_approval(db, world, monkeypatch):
    """The board's own event writer cuts text at 5000 characters, which would leave the stored call unparseable and unrunnable."""
    user, proj, bot = world
    args = {"projectKey": "ABC", "summary": "Big", "description": "line\n" * 3000}
    queue_outward(bot.id, "createJiraIssue", args, "ABC", "", db, interactive=True)
    task = _task(db, bot)
    shown = _events(db, task)[0]
    assert len(shown) > 5000 and shown.rstrip().endswith("~~~") and "\n• Create" in shown  # not cut off, and its layout is kept

    ran, _ = _decide(db, user, proj, task, "Approved", monkeypatch)

    assert ran == [("createJiraIssue", args, "ABC", "", bot.id)]


def test_a_call_too_large_to_show_is_refused(db, world):
    user, proj, bot = world
    text, event = queue_outward(bot.id, "createJiraIssue", {"description": "x" * (outbox.MAX_CALL_CHARS + 1)}, "ABC", "", db, interactive=True)
    assert text.startswith("Error: that call is too large") and event is None and db.query(Task).count() == 0


def test_requests_in_one_turn_become_one_task_and_one_card(db, world):
    user, proj, bot = world
    turn = {}
    a = {"issueIdOrKey": "ABC-1", "commentBody": "first"}
    b = {"issueIdOrKey": "ABC-2", "commentBody": "second"}
    queue_outward(bot.id, "addCommentToJiraIssue", a, "ABC", "", db, interactive=True, turn=turn)
    queue_outward(bot.id, "addCommentToJiraIssue", b, "ABC", "", db, interactive=True, turn=turn)
    queue_outward(bot.id, "gmail_send_message", {"to": "a@b.co", "subject": "Hi", "body": "x"}, "", "", db, interactive=True, turn=turn)

    (task,) = db.query(Task).filter(Task.agent_id == bot.id).all()
    assert task.title == "Approve 3 actions" and task.status == "decision"
    first = _events(db, task)[0]
    assert first.startswith("Needs your permission to do these 3 things.\n• Comment on ABC-1 in Jira\n• Comment on ABC-2 in Jira\n• Send email: Hi\n")
    assert "first" in first and "second" in first
    stored = json.loads(_events(db, task)[-1][len(MAIL_DRAFT):])
    assert [c["tool"] for c in stored["calls"]] == ["addCommentToJiraIssue", "addCommentToJiraIssue", "gmail_send_message"]
    assert stored["calls"][0] == {"tool": "addCommentToJiraIssue", "args": a, "jira_key": "ABC", "repo": ""}

    (card,) = _card_of(db, bot)  # one card, updated in place, not one per request
    assert (card["task_id"], card["count"], card["title"], card["name"]) == (task.id, 3, "Approve 3 actions", bot.name)
    assert sum(1 for _ in _events(db, task) if _.startswith(MAIL_DRAFT)) == 1


def test_one_approval_runs_every_call_in_order_with_its_own_scope(db, world, monkeypatch):
    user, proj, bot = world
    turn = {}
    a = {"issueIdOrKey": "ABC-1", "commentBody": "first"}
    b = {"issueIdOrKey": "XYZ-9", "commentBody": "second"}
    queue_outward(bot.id, "addCommentToJiraIssue", a, "ABC", "", db, interactive=True, turn=turn)
    queue_outward(bot.id, "addCommentToJiraIssue", b, "XYZ", "", db, interactive=True, turn=turn)

    ran, kicked = _decide(db, user, proj, _task(db, bot), "Approved for this session", monkeypatch)

    assert ran == [("addCommentToJiraIssue", a, "ABC", "", bot.id), ("addCommentToJiraIssue", b, "XYZ", "", bot.id)]
    assert len(kicked) == 1 and "actions were approved" in kicked[0][3]


def test_declining_a_group_runs_none_of_it(db, world, monkeypatch):
    user, proj, bot = world
    turn = {}
    for key in ("ABC-1", "ABC-2"):
        queue_outward(bot.id, "addCommentToJiraIssue", {"issueIdOrKey": key, "commentBody": "x"}, "ABC", "", db, interactive=True, turn=turn)
    ran, _ = _decide(db, user, proj, _task(db, bot), "Declined", monkeypatch)
    assert ran == []


def test_a_new_turn_or_an_answered_task_starts_a_new_card(db, world, monkeypatch):
    user, proj, bot = world
    turn = {}
    queue_outward(bot.id, "addCommentToJiraIssue", {"issueIdOrKey": "ABC-1", "commentBody": "x"}, "ABC", "", db, interactive=True, turn=turn)
    queue_outward(bot.id, "addCommentToJiraIssue", {"issueIdOrKey": "ABC-2", "commentBody": "x"}, "ABC", "", db, interactive=True, turn={})  # another turn
    assert db.query(Task).count() == 2
    _decide(db, user, proj, db.query(Task).order_by(Task.created_at).first(), "Declined", monkeypatch)  # answered meanwhile
    queue_outward(bot.id, "addCommentToJiraIssue", {"issueIdOrKey": "ABC-3", "commentBody": "x"}, "ABC", "", db, interactive=True, turn=turn)
    assert db.query(Task).count() == 3 and len(_card_of(db, bot)) == 3


def test_joining_a_turns_task_does_not_hit_the_boards_limit(db, world, monkeypatch):
    """A bot on its own parks its first call, which itself fills the board to the limit; the next call in the turn joins it rather than being refused."""
    user, proj, bot = world
    seen = []
    monkeypatch.setattr(outbox, "decision_cap_msg", lambda agent_id, db: None if not seen else "NOT queued: full")
    turn = {}
    first, _ = queue_outward(bot.id, "createJiraIssue", {"summary": "a"}, "ABC", "", db, turn=turn)
    seen.append(1)
    second, _ = queue_outward(bot.id, "createJiraIssue", {"summary": "b"}, "ABC", "", db, turn=turn)
    assert first.startswith("NOT done") and second.startswith("NOT done") and db.query(Task).count() == 1
    other, event = queue_outward(bot.id, "createJiraIssue", {"summary": "c"}, "ABC", "", db, turn={})  # a different turn is held to the limit
    assert other.startswith("NOT queued") and event is None


def test_a_big_push_is_blocked_for_a_bot_alone_but_shown_to_a_person(db, world):
    user, proj, bot = world
    files = [{"path": f"f{i}.txt", "content": "x"} for i in range(outbox.PUSH_MAX_FILES + 1)]
    args = {"owner": "o", "repo": "r", "branch": "b", "files": files, "message": "many"}

    alone, _ = queue_outward(bot.id, "github_push_files", args, "", "o/r", db)
    assert alone.startswith("BLOCKED") and _task(db, bot).status == "blocked"

    asked, _ = queue_outward(bot.id, "github_push_files", args, "", "o/r", db, interactive=True)
    assert "a card in this chat" in asked and sorted(t.status for t in db.query(Task).all()) == ["blocked", "decision"]


def test_an_approved_action_runs_as_the_person_who_approved_it(db, world, monkeypatch):
    """The approval route is not a chat route, so nothing set who was acting. Connected accounts (Jira, GitHub, Gmail) are looked up for that
    person, and a background job the action starts (the wiki build) reports back into their chat."""
    from core.usage import current_user
    user, proj, bot = world
    queue_outward(bot.id, "addCommentToJiraIssue", {"issueIdOrKey": "ABC-1", "commentBody": "x"}, "ABC", "", db, interactive=True)
    from core import usage
    outer = usage._user_id.set("")
    seen = []

    async def fake_run_tool(*a):
        seen.append(current_user())
        return "ok"
    from routes import tasks
    monkeypatch.setattr(tasks, "run_tool", fake_run_tool)
    monkeypatch.setattr("routes.chat._start_kickoff", lambda *a, **k: None)
    try:
        asyncio.run(tasks._apply_decision(_task(db, bot), "Approved", user, proj, db))
        after = current_user()
    finally:
        usage._user_id.reset(outer)
    assert seen == [user.id]  # the approver, while the action ran
    assert after == ""  # and only then
