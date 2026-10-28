"""GitHub PR webhooks: signed deliveries only, handed to the project's coding bot."""
import asyncio
from datetime import datetime, timezone
import hashlib
import hmac
import json
import urllib.parse

import pytest
from fastapi import HTTPException

import routes.chat as chat
from core.models import Agent, Message, Project, ProjectMember, User
from routes import webhooks
from services import code_review, followup, github_app, runs


class Req:
    def __init__(self, payload, event="pull_request", sign=True, form=False):
        self._body = json.dumps(payload).encode()
        ctype = "application/json"
        if form:
            self._body, ctype = b"payload=" + urllib.parse.quote(self._body).encode(), "application/x-www-form-urlencoded"
        sig = "sha256=" + hmac.new(b"s3cret", self._body, hashlib.sha256).hexdigest() if sign else "sha256=bad"
        self.headers = {"X-Hub-Signature-256": sig, "X-GitHub-Event": event, "Content-Type": ctype}

    async def body(self):
        return self._body


def pr(action, repo="acme/repo", draft=False):
    return {"action": action, "repository": {"full_name": repo}, "pull_request": {"number": 7, "title": "Fix <it>", "draft": draft, "user": {"login": "bob"}}}


def hit(req):
    async def go():
        out = await webhooks.github_webhook(req)
        await asyncio.gather(*runs._tasks)  # the review runs in the background
        return out
    return asyncio.run(go())


@pytest.fixture
def started(db, monkeypatch):
    monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", "s3cret")
    monkeypatch.delenv("CODE_REVIEW_POST", raising=False)
    calls = []
    monkeypatch.setattr(chat, "_start_kickoff", lambda *a, **k: calls.append(a + (k.get("on_done"),)))
    code_review._seen.clear()

    async def no_emails(repo, number, login, owner):
        return set()  # no test reaches GitHub for the author's email unless it says so
    monkeypatch.setattr(webhooks, "_pr_emails", no_emails)

    async def fake_review(repo, pr, user_id, context=""):
        return {"summary": "Looks fine.", "overall": "Fix T first.", "verdict": "comment", "files": 1, "not_reviewed": [],
                "findings": [{"file": "a.py", "line": 3, "end_line": 0, "severity": "major", "category": "security", "title": "T", "why": "B", "fix": "F"}]}
    monkeypatch.setattr(code_review, "review", fake_review)
    monkeypatch.setattr(code_review, "publish_doc", lambda *a: ("PR #7 review: Fix <it>", "pr-7-review-20261028-100000.md"))
    monkeypatch.setattr(code_review, "publish_fix_doc", lambda *a: ("PR #7 fix prompt: Fix <it>", "pr-7-fix-prompt-20261028-100000.md"))
    db.add_all([User(id="u", email="a@b.c", name="Ann", password_hash="x"),
                Project(id="p", name="P", repo="Acme/Repo", coding_bot_id="a"),
                Project(id="q", name="Q", repo="acme/other", coding_bot_id="")])
    db.flush()
    db.add_all([Agent(id="a", name="Coder", system_prompt="x", project_id="p"), ProjectMember(project_id="p", user_id="u", role="owner")])
    db.commit()
    return calls


def chat_rows(db):
    db.expire_all()
    return [m.content for m in db.query(Message).filter(Message.agent_id == "a", Message.user_id == "u").order_by(Message.id)]


def test_pr_reaches_the_coding_bot(started, db):
    assert hit(Req(pr("opened"))) == {"ok": True, "notified": 1}  # repo match ignores case
    agent_id, source, project_id, text, user_id, _done = started[0]
    assert (agent_id, project_id, user_id) == ("a", "p", "u") and "BEGIN REVIEW" in text and "[major] a.py:3" in text  # the bot is handed the findings
    assert "saved in Docs" in text
    card, doc, fix = chat_rows(db)  # the pill was updated in place, and the review and the fix prompt follow as documents of their own
    assert card.startswith('[[SOURCES:{"kind": "pr"') and '"status": "done"' in card and '"findings": 1' in card and '"verdict": "comment"' in card
    assert doc == "[PR #7 review: Fix <it>](/api/documents/pr-7-review-20261028-100000.md)"
    assert fix == "[PR #7 fix prompt: Fix <it>](/api/documents/pr-7-fix-prompt-20261028-100000.md)"


def test_the_pill_spins_until_the_review_ends(started, db, monkeypatch):
    seen = []

    async def slow_review(repo, pr, user_id, context=""):
        seen.append(chat_rows(db))  # what the chat shows while the review runs
        raise RuntimeError("Muse failed to run")
    monkeypatch.setattr(code_review, "review", slow_review)
    hit(Req(pr("opened")))
    assert len(seen[0]) == 1 and '"status": "running"' in seen[0][0]
    rows = chat_rows(db)
    assert len(rows) == 1 and '"status": "failed"' in rows[0] and "Muse failed to run" in rows[0]  # no document row for a failed review
    assert "could not run: Muse failed to run" in started[0][3]


def test_rejects_bad_signature_and_missing_secret(started, monkeypatch):
    with pytest.raises(HTTPException) as e:
        hit(Req(pr("opened"), sign=False))
    assert e.value.status_code == 401
    monkeypatch.delenv("GITHUB_WEBHOOK_SECRET")
    with pytest.raises(HTTPException):
        hit(Req(pr("opened")))
    assert not started


def test_ignores_what_it_should(started):
    assert (hit(Req(pr("opened", repo="acme/other"))))["notified"] == 0  # no coding bot chosen
    assert hit(Req(pr("closed"))) == {"ok": True}
    assert (hit(Req(pr("opened", draft=True))))["skipped"] == "draft"
    assert hit(Req({"zen": "hi"}, event="ping")) == {"ok": True}
    assert not started


def test_accepts_form_encoded_deliveries(started):
    assert hit(Req(pr("opened"), form=True)) == {"ok": True, "notified": 1}  # GitHub's default content type


def test_a_redelivered_head_is_reviewed_once(started):
    payload = pr("opened")
    payload["pull_request"]["head"] = {"sha": "abc"}
    assert hit(Req(payload))["notified"] == 1 and hit(Req(payload))["notified"] == 0 and len(started) == 1
    payload["pull_request"]["head"] = {"sha": "def"}  # a new push is a new review
    assert hit(Req(payload))["notified"] == 1


def test_clean_trims_and_sorts_a_review():
    raw = json.dumps({"summary": "s", "verdict": "bogus", "overall": "o", "reviewed_files": ["a"], "findings": [
        {"file": "a", "line": 1, "severity": "nit", "category": "security", "title": "t", "why": "w", "fix": "f"},
        {"file": "b", "line": 2, "severity": "blocker", "category": "nonsense", "title": "t", "why": "w", "fix": ""},
        {"file": "c", "line": 3, "severity": "made-up", "title": "t", "why": "w"}]})
    got = code_review.clean_findings(raw)
    assert got["verdict"] == "comment" and [f["file"] for f in got["findings"]] == ["b", "a"] and got["findings"][0]["category"] == "correctness"
    with pytest.raises(ValueError):
        code_review.clean_findings("[1]")

def test_review_is_posted_only_when_switched_on(started, monkeypatch):
    posted = []

    async def post(repo, number, pr, result):
        posted.append((repo, number, pr['head']['sha']))
        return 1
    monkeypatch.setattr(github_app, "post_review", post)
    monkeypatch.setattr(github_app, "configured", lambda: True)
    payload = pr("opened")
    payload["pull_request"]["head"] = {"sha": "abc"}
    monkeypatch.delenv("CODE_REVIEW_POST", raising=False)
    hit(Req(payload))
    assert posted == [] and "posted on the PR" not in started[0][3]
    monkeypatch.setenv("CODE_REVIEW_POST", "1")
    payload["pull_request"]["head"] = {"sha": "def"}
    hit(Req(payload))
    assert posted == [("acme/repo", 7, "def")] and "posted on the PR" in started[1][3]


def test_batches_cover_every_file_without_a_cap():
    files = [{"path": f"f{i}", "status": "modified", "patch": "x" * n} for i, n in enumerate((60, 60, 500, 10))]
    groups = code_review.batches(files, limit=100)
    assert [[f["path"] for f in g] for g in groups] == [["f0"], ["f1"], ["f2"], ["f3"]]  # an oversized file gets its own run
    assert [f["path"] for g in code_review.batches(files, limit=10_000) for f in g] == ["f0", "f1", "f2", "f3"]


FINDING = {"file": "x.py", "line": 12, "end_line": 0, "severity": "major", "category": "security", "title": "Bad", "why": "Because", "fix": "Do this"}


def test_merge_keeps_the_worst_verdict_and_names_uncovered_files():
    one = {"summary": "a", "overall": "", "verdict": "comment", "findings": [FINDING]}
    two = {"summary": "b", "overall": "", "verdict": "approve", "findings": [dict(FINDING)]}  # same finding twice
    got = code_review.merge([one, two], ["big.bin"], 5)
    assert got["verdict"] == "request_changes" and len(got["findings"]) == 1 and "Not reviewed (1 of 5 files): big.bin" in got["summary"]
    assert got["overall"] == "Not ready to merge: Bad."


def test_mermaid_is_made_safe_and_junk_is_dropped():
    from services import review_render as render
    code = "```mermaid\nsequenceDiagram\n    C->>R: POST /a; then #1 %%x\n    R-->>C: ok\n```"
    assert render.diagram(code) == "sequenceDiagram\n    C->>R: POST /a, then 1 x\n    R-->>C: ok"
    assert render.diagram("this is not a diagram") == "" and render.diagram("") == ""
    assert render.diagram("graph TD\n A-->B").startswith("graph TD")


def test_findings_are_anchored_to_changed_lines_only():
    from services import review_render as render
    diff = "diff --git a/x.py b/x.py\n(modified)\n@@ -10,3 +10,4 @@\n ctx\n+new\n+newer\n ctx2\n"
    lines = render.commentable_lines(diff)
    assert lines == {"x.py": {10, 11, 12, 13}}
    inside = {**FINDING, "line": 11, "end_line": 12}
    outside = {**FINDING, "line": 99, "title": "Far away"}
    wrong_file = {**FINDING, "file": "nope.py", "title": "Elsewhere"}
    comments, loose = render.anchor([inside, outside, wrong_file], lines)
    assert [(c["path"], c["start_line"], c["line"]) for c in comments] == [("x.py", 11, 12)]  # a range only when all of it is commentable
    assert [f["title"] for f in loose] == ["Far away", "Elsewhere"]
    assert render._resolve("pkg/x.py", {"src/pkg/x.py": {1}}) == "src/pkg/x.py"  # monorepo-relative paths still match
    body = comments[0]["body"]
    assert "⚠️ **major** · security · `1/3`" in body and "**Bad**" in body and "**Suggested fix:** Do this" in body


def test_summary_walkthrough_and_fix_prompt_read_like_the_original_bot():
    from services import review_render as render
    result = {"summary": "S", "overall": "Fix Bad first.", "verdict": "request_changes", "findings": [FINDING, {**FINDING, "severity": "nit", "title": "Tiny", "line": 40}],
              "files": 2, "not_reviewed": ["big.bin"]}
    summary = render.summary_md(result, 1, [result["findings"][1]], "abcdef1234567")
    for part in ("## 🤖 Review — ⚠️ 1 major · 🔹 1 nit", "Fix Bad first.", "1 inline, 1 not on a changed line", "### Not anchored to a changed line", "- **nit** — Tiny", "`big.bin`", "Reviewed `abcdef12`"):
        assert part in summary, part
    wt = {"summary": "Adds X.", "changes": [{"file": "a|b.py", "what": "adds\nX", "intent": "because"}], "theme": "five\nseven five",
          "sequence_diagram": "sequenceDiagram\n A->>B: hi", "dependency_graph": "graph TD\n A-->B", "blast_radius": "",
          "plan": {"high_risk": ["auth"], "caller_chains": [], "edge_cases": [], "patterns": [], "safe_to_skip": ["docs"]}}
    md = render.walkthrough_md(wt)
    for part in ("## 🤖 Walkthrough", "| `a\\|b.py` | adds X | because |", "> five", "```mermaid\nsequenceDiagram", "### Dependency graph", "**High-risk areas**", "- auth", "**Safe to skip**"):
        assert part in md, part
    assert "Blast radius" not in md and "Caller chains" not in md  # empty sections are left out
    pr_ = {"number": 7, "title": "Fix it", "html_url": "u", "head": {"ref": "h", "sha": "abcdef1234567"}, "base": {"ref": "b"}}
    prompt = render.fix_prompt("acme/repo", pr_, result["findings"])
    assert "Fix the review findings on acme/repo#7 (Fix it)." in prompt and "1. [MAJOR] Bad" in prompt and "Location: `x.py:12`" in prompt and "Suggested fix: Do this" in prompt
    assert render.fix_prompt("acme/repo", pr_, []) == "" and render.fence("a ```b``` c").startswith("````text\n") and render.fence("a ````b```` c").startswith("`````text\n")


def test_review_doc_has_everything_the_pr_comments_have():
    from services import review_render as render
    pr_ = {"number": 7, "title": "Fix it", "user": {"login": "bob"}, "head": {"ref": "h", "sha": "abcdef1234567"}, "base": {"ref": "b"}, "html_url": "u"}
    result = {"summary": "S", "overall": "Fix Bad first.", "verdict": "request_changes", "files": 3, "not_reviewed": ["big.bin"], "findings": [FINDING],
              "walkthrough": {"summary": "Adds X.", "changes": [], "theme": "", "sequence_diagram": "sequenceDiagram\n A->>B: hi", "dependency_graph": "", "blast_radius": "", "plan": {}}}
    md = render.doc_markdown("acme/repo", pr_, result, "Coder")
    for part in ("# PR #7: Fix it", "**Verdict:** request changes", "2 of 3 changed files", "Fix Bad first.", "## Walkthrough", "```mermaid", "## ⚠️ Major (1)", "### Bad",
                 "`x.py:12` · security", "Because", "**Suggested fix:** Do this", "## Not reviewed", "`big.bin`", "Written by Coder"):
        assert part in md, part


def test_blast_radius_headings_nest_under_their_section():
    got = code_review.clean_walkthrough(json.dumps({"summary": "s", "blast_radius": "### Entry points\n- a\n## Direct dependents\n- b", "plan": {}}))
    assert got["blast_radius"] == "#### Entry points\n- a\n#### Direct dependents\n- b" and got["changes"] == [] and got["sequence_diagram"] == ""


def test_poll_reviews_only_prs_without_a_review_yet(started, db, monkeypatch):
    from services import pr_poll
    fresh = {"number": 11, "title": "New", "draft": False, "user": {"login": "bob"}, "head": {"ref": "h", "sha": "aaa"}, "base": {"ref": "b"}, "html_url": "u",
             "updated_at": "2026-10-28T10:00:00Z"}
    prs = [fresh,
           {**fresh, "number": 12, "draft": True},                                    # drafts wait
           {**fresh, "number": 13, "updated_at": "2026-09-01T00:00:00Z"},              # untouched for weeks
           {**fresh, "number": 14}]                                                    # already has a pill without a commit (from before pills had one)
    db.add(Message(agent_id="a", user_id="u", role="assistant", content=webhooks._card("Acme/Repo", {"number": 14, "title": "Old"}, "opened").replace('"sha": "", ', "")))
    db.commit()

    async def listing(repo, token):
        return prs

    async def token(repo, owner):
        return "t"
    monkeypatch.setattr(pr_poll, "open_prs", listing)
    monkeypatch.setattr(code_review, "_token", token)
    real_recent = pr_poll.recent
    monkeypatch.setattr(pr_poll, "recent", lambda pr, now=None: real_recent(pr, datetime(2026, 10, 28, 12, tzinfo=timezone.utc)))

    async def go():
        n = await pr_poll.poll_once()
        await asyncio.gather(*runs._tasks)
        return n
    assert asyncio.run(go()) == 1  # only #11
    assert [json.loads(c[10:-2])["number"] for c in chat_rows(db) if '"status": "done"' in c] == [11]
    code_review._seen.clear()
    assert asyncio.run(go()) == 0  # polling again finds a pill for #11 at that commit
    assert webhooks.seen_before(db, "a", "u", "acme/repo", 11, "aaa") and not webhooks.seen_before(db, "a", "u", "acme/repo", 11, "bbb")  # a new push is new


def test_the_author_gets_the_pill_the_document_and_the_bots_words_in_their_own_chat(started, db, monkeypatch):
    db.add_all([User(id="w", email="w@b.c", name="Wen", password_hash="x"), User(id="z", email="z@b.c", name="Zed", password_hash="x")])
    db.flush()
    db.add_all([ProjectMember(project_id="p", user_id="w", role="member"), ProjectMember(project_id="p", user_id="z", role="member")])
    db.commit()

    async def login(user_id):
        return {"w": "bob", "u": "ann"}.get(user_id, "")  # Wen is bob on GitHub; Zed has not connected GitHub
    monkeypatch.setattr(webhooks, "_github_login", login)

    def rows(user_id):
        db.expire_all()
        return [m.content for m in db.query(Message).filter(Message.agent_id == "a", Message.user_id == user_id).order_by(Message.id)]
    hit(Req(pr("opened")))  # opened by bob
    mine, theirs, other = rows("u"), rows("w"), rows("z")
    assert len(mine) == len(theirs) == 3 and '"status": "done"' in theirs[0] and theirs[1].startswith("[PR #7 review:") and theirs[2].startswith("[PR #7 fix prompt:") and other == []  # Zed is not the author
    started[0][-1]("Found one real problem, details in the doc.", "")  # the bot finishes its reply to the owner
    assert rows("w")[-1] == "Found one real problem, details in the doc." and len(rows("u")) == 3

    hit(Req({**pr("opened"), "pull_request": {**pr("opened")["pull_request"], "number": 8, "user": {"login": "outsider"}}}))
    assert len(rows("w")) == 4 and rows("z") == []  # an outside contributor is on no one's list


def test_the_switch_stops_reviews_from_the_webhook_and_the_poll(started, db, monkeypatch):
    from core.models import Project
    from routes.projects import PrReviewsBody, set_pr_reviews
    from services import pr_poll
    owner, member = db.get(User, "u"), User(id="m", email="m@b.c", name="Max", password_hash="x")
    db.add(member)
    db.flush()
    db.add(ProjectMember(project_id="p", user_id="m", role="member"))
    db.commit()
    with pytest.raises(HTTPException) as e:
        set_pr_reviews("p", PrReviewsBody(enabled=False), member, db)
    assert e.value.status_code == 403  # only the owner flips it

    assert set_pr_reviews("p", PrReviewsBody(enabled=False), owner, db) == {"pr_reviews": False}
    assert db.get(Project, "p").pr_reviews is False
    assert hit(Req(pr("opened")))["notified"] == 0 and not started and chat_rows(db) == []
    assert pr_poll.targets() == {}  # the poll skips the project too

    set_pr_reviews("p", PrReviewsBody(enabled=True), owner, db)
    assert hit(Req(pr("opened")))["notified"] == 1 and list(pr_poll.targets()) == ["acme/repo"]


def test_a_posted_comment_reads_back_as_the_same_finding():
    from services import review_render as render
    body = render.inline_body(FINDING, 2, 5)
    got = render.parse_thread({"id": "T1", "path": "x.py", "line": 12, "body": body}, 3)
    assert got == {"id": "F3", "thread": "T1", "file": "x.py", "line": 12, "end_line": 0, "severity": "major", "category": "security",
                   "title": "Bad", "why": "Because", "fix": "Do this"}
    assert render.parse_thread({"id": "T2", "path": "x.py", "line": 1, "body": "someone typed this by hand"}, 1) is None
    no_fix = render.parse_thread({"id": "T3", "path": "x.py", "line": 1, "body": render.inline_body({**FINDING, "fix": ""}, 1, 1)}, 1)
    assert no_fix["fix"] == "" and no_fix["why"] == "Because"


def test_follow_up_sorts_earlier_findings_into_fixed_and_open_and_flags_only_new_issues():
    from services import followup
    prev = [{**FINDING, "id": "F1", "thread": "T1", "title": "Old bug"}, {**FINDING, "id": "F2", "thread": "T2", "title": "Other bug", "severity": "minor"},
            {**FINDING, "id": "F3", "thread": "T3", "title": "Unchecked", "severity": "minor"}]
    statuses = {"F1": {"status": "fixed", "note": "gone"}, "F2": {"status": "still present", "note": "same"}}  # the model skipped F3
    new_ = [{**FINDING, "title": "New bug", "severity": "minor"}, {**FINDING, "title": "Pedantry", "severity": "nit"}, {**FINDING, "title": "Other bug", "severity": "minor"}]
    got = followup.assemble(prev, statuses, [{"summary": "s", "findings": new_}], [], 2, "abc12345", "diff")
    assert [f["title"] for f in got["fixed"]] == ["Old bug"] and got["fixed_threads"] == ["T1"]
    assert [f["title"] for f in got["still_open"]] == ["Other bug", "Unchecked"] and got["still_open"][1]["note"] == "Could not be re-checked."  # unsure is never "fixed"
    assert [f["title"] for f in got["findings"]] == ["New bug"]  # no nits, and an earlier finding is not reported again as new
    assert got["verdict"] == "comment" and got["overall"] == "No blockers, but 3 items are still open."
    clean = followup.assemble(prev[:1], {"F1": {"status": "fixed", "note": ""}}, [{"summary": "s", "findings": []}], [], 1, "abc", "")
    assert clean["verdict"] == "approve" and clean["overall"].startswith("Ready to merge")
    bad = followup.assemble([{**prev[0], "severity": "blocker"}], {"F1": {"status": "partially fixed", "note": "half"}}, [], [], 0, "abc", "")
    assert bad["verdict"] == "request_changes" and bad["overall"] == "Not ready to merge: Old bug."


def test_follow_up_summary_and_doc_say_what_got_fixed():
    from services import followup, review_render as render
    prev = [{**FINDING, "id": "F1", "thread": "T1", "title": "Old bug"}, {**FINDING, "id": "F2", "thread": "T2", "title": "Still bad"}]
    res = followup.assemble(prev, {"F1": {"status": "fixed", "note": ""}, "F2": {"status": "still present", "note": "not touched"}},
                            [{"summary": "Checked the push.", "findings": [{**FINDING, "title": "Fresh"}]}], [], 1, "abcdef1234567", "")
    md = render.followup_summary_md(res, 1, [], "fedcba9876543")
    for part in ("## 🤖 Follow-up review — ✅ 1 fixed · 1 still open · 1 new", "### ✅ Fixed since last review (1)", "- Old bug", "### Still open (1)", "Still bad", "not touched",
                 "### New in this push (1)", "1 inline", "Reviewed `fedcba98`, the changes since `abcdef12`"):
        assert part in md, part
    pr_ = {"number": 7, "title": "Fix it", "html_url": "u", "head": {"ref": "h", "sha": "fedcba9876543"}, "base": {"ref": "b"}}
    doc = render.followup_doc_markdown("acme/repo", pr_, res, "Coder")
    for part in ("# PR #7 follow-up review: Fix it", "1 fixed, 1 still open", "## ✅ Fixed since the last review", "## Still open", "## New: ⚠️ Major (1)"):
        assert part in doc, part


def test_a_push_after_a_finished_review_gets_a_follow_up_and_a_failed_compare_falls_back_to_a_full_review(started, db, monkeypatch):
    calls = []

    async def fake_followup(repo, pr, owner, context, since):
        calls.append(since)
        return None if since == "gone" else {"followup": True, "since": since, "summary": "s", "overall": "Ready to merge: fine.", "verdict": "approve", "findings": [], "fixed": [{"title": "F"}],
                                             "still_open": [], "fixed_threads": [], "files": 1, "not_reviewed": [], "diff": ""}
    monkeypatch.setattr(followup, "review", fake_followup)
    monkeypatch.setattr(code_review, "publish_doc", lambda *a: ("T", "pr-7-followup-1.md"))
    first = pr("opened")
    first["pull_request"]["head"] = {"sha": "aaa"}
    hit(Req(first))
    assert calls == [] and webhooks.last_reviewed_sha("a", "u", "acme/repo", 7) == "aaa"  # a first review is a full one

    second = pr("synchronize")
    second["pull_request"]["head"] = {"sha": "bbb"}
    hit(Req(second))
    assert calls == ["aaa"]  # new commit: the follow-up checks what changed since the last reviewed one
    rows = chat_rows(db)
    card = next(r for r in rows if '"sha": "bbb"' in r)
    assert '"followup": true' in card and '"fixed": 1' in card and '"open": 0' in card and '"verdict": "approve"' in card
    assert "1 earlier finding(s) fixed" in started[-1][3]

    third = pr("synchronize")
    third["pull_request"]["head"] = {"sha": "ccc"}
    monkeypatch.setattr(webhooks, "last_reviewed_sha", lambda *a: "gone")  # the earlier commit vanished in a force-push
    hit(Req(third))
    assert calls == ["aaa", "gone"] and "BEGIN REVIEW" in started[-1][3] and "re-reviewed" not in started[-1][3]  # fell back to a whole-PR review


def test_comments_in_the_original_bots_format_read_back_too():
    from services import review_render as render
    old = "⚠️ **major** · `2/16`\n\n**DM auto-share grants access**\n_status: still present_\n\nNever uses sender_id.\n\n**Suggested fix:** Check the owner."
    got = render.parse_thread({"id": "T9", "path": "p.py", "line": 534, "body": old}, 2)
    assert (got["severity"], got["category"], got["title"], got["why"], got["fix"]) == ("major", "correctness", "DM auto-share grants access", "Never uses sender_id.", "Check the owner.")


def test_the_fix_prompt_is_its_own_plain_markdown_document(db, tmp_path, monkeypatch):
    from core.models import DocumentRecord
    from tools import documents
    monkeypatch.setattr(documents, "DOC_DIR", tmp_path)
    monkeypatch.setattr(documents, "_r2", lambda: None)
    db.add_all([User(id="u", email="a@b.c", name="Ann", password_hash="x"), User(id="v", email="d@e.f", name="Dan", password_hash="x"), Project(id="p", name="P")])
    db.flush()
    db.add(Agent(id="a", name="Coder", system_prompt="x", project_id="p"))
    db.commit()
    pr_ = {"number": 7, "title": "Fix it", "html_url": "u", "head": {"ref": "h", "sha": "abc"}, "base": {"ref": "b"}}
    result = {"findings": [FINDING], "still_open": [{**FINDING, "title": "Older bug"}]}
    title, name = code_review.publish_fix_doc("a", "p", "u", ["u", "v"], "acme/repo", pr_, result)
    text = (tmp_path / name).read_text()
    assert title == "PR #7 fix prompt: Fix it" and text.startswith("# PR #7 fix prompt: Fix it") and "```" not in text  # plain, not a code block
    assert "1. [MAJOR] Older bug" in text and "2. [MAJOR] Bad" in text and "Location: `x.py:12`" in text  # open and new findings both
    assert db.get(DocumentRecord, name).shared_with == ["v"]
    assert code_review.publish_fix_doc("a", "p", "u", ["u"], "acme/repo", pr_, {"findings": [], "still_open": []}) is None  # nothing to fix, no document


def test_an_author_with_no_github_connection_is_found_by_the_email_on_their_commits(started, db, monkeypatch):
    db.add_all([User(id="w", email="Wen@Neuron.in", name="Wen", password_hash="x"), User(id="z", email="z@neuron.in", name="Zed", password_hash="x")])
    db.flush()
    db.add_all([ProjectMember(project_id="p", user_id="w", role="member"), ProjectMember(project_id="p", user_id="z", role="member")])
    db.commit()

    async def nobody(user_id):
        return ""  # nobody has connected GitHub
    monkeypatch.setattr(webhooks, "_github_login", nobody)
    asked = []

    async def emails(repo, number, login, owner):
        asked.append(login)
        return {"wen@neuron.in", "someone.else@x.y"}  # the case of an email does not matter
    monkeypatch.setattr(webhooks, "_pr_emails", emails)
    hit(Req(pr("opened")))
    db.expire_all()
    got = {uid: db.query(Message).filter(Message.agent_id == "a", Message.user_id == uid).count() for uid in ("u", "w", "z")}
    assert asked == ["bob"] and got == {"u": 3, "w": 3, "z": 0}  # the owner still gets everything, Wen gets hers, Zed gets nothing

    async def none(repo, number, login, owner):
        return set()
    monkeypatch.setattr(webhooks, "_pr_emails", none)
    hit(Req({**pr("opened"), "pull_request": {**pr("opened")["pull_request"], "number": 9}}))
    db.expire_all()
    assert db.query(Message).filter(Message.agent_id == "a", Message.user_id == "w").count() == 3  # no email match: only the owner
    assert webhooks._member_with_email(["u", "w"], "u", {"u@b.c"}) == ""  # the owner is never "the author": they get everything already
