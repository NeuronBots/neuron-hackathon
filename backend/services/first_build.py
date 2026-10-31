"""Muse builds a project's first version from its approved plan, in a sandbox.

The plan the Master wrote gets a button under it. When the person clicks it (their approval of the plan), the plan and the project's repository go into a
Modal sandbox (services/build_sandbox.py) where Muse writes the code with the ponytail, frontend-design and single-responsibility skills loaded, runs it and
writes a run guide from commands it actually ran. The code comes back as a zip and the run guide as a document in Docs; the Master gives the person the guide's
exact commands; and, when the project has a repository, a pull request is offered behind the same Allow/Deny card as any other outward write.
Nothing generated is ever run on this server."""
from __future__ import annotations

import asyncio
import io
import logging
import os
import re
import tarfile
import time

from services import build_prompt, chat_rows, code_review as cr, imagegen, runs, starter
from services.build_sandbox import BuildBox

log = logging.getLogger(__name__)

EFFORT = os.getenv("BUILD_EFFORT", "high")
TIMEOUT = int(os.getenv("BUILD_TIMEOUT_SECONDS", "7200"))  # Muse's run; the sandbox itself lives a little longer
PARALLEL = int(os.getenv("BUILD_PARALLEL", "2"))  # builds at once on this server; each holds a sandbox for a long time
TICK_SECONDS = 45
MAX_REVIEWS = int(os.getenv("BUILD_MAX_REVIEWS", "2"))  # review rounds; the last one only reports
_slots = asyncio.Semaphore(PARALLEL)
_running: dict[str, asyncio.Task] = {}  # project id -> its build

class BuildError(ValueError):
    """The build was refused, with what the person should hear and the HTTP status that goes with it."""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


_DOC = re.compile(r"/api/documents/([A-Za-z0-9][A-Za-z0-9._-]{0,120}\.md)")
_TRIVIAL = {"readme.md", "readme", "license", "license.md", ".gitignore", ".gitattributes"}


# ── the button under the plan ────────────────────────────────────────────

def offer_build(agent_id: str, made: str, title: str) -> None:
    """A plan was just written: put a card under it whose button starts the build. Runs in a thread; never raises (a missing button must not fail the tool)."""
    from core.database import SessionLocal
    from core.models import Agent
    from core.usage import current_user

    try:
        found, user = _DOC.search(made or ""), current_user()
        if not found or not user:
            return
        db = SessionLocal()
        try:
            agent = db.get(Agent, agent_id)
            project_id = agent.project_id if agent else ""
        finally:
            db.close()
        if not project_id:
            return
        fields = {"filename": found.group(1), "title": (title or "")[:200], "project_id": project_id}
        row = chat_rows.write_row(agent_id, user, chat_rows.card("buildplan", status="ready", **fields))
        chat_rows.write_row(agent_id, user, chat_rows.card("buildplan", status="ready", row=row, **fields), row)  # now it knows its own row, to say it started
    except Exception:
        log.exception("Could not offer to build the plan")


def _mark_started(agent_id: str, user_id: str, row_id: int) -> None:
    """The plan's card says the build started (and so loses its button), if the row really is a plan card in this person's chat with this bot."""
    import json

    from core.database import SessionLocal
    from core.models import Message

    db = SessionLocal()
    try:
        row = db.get(Message, row_id)
        if row is None or row.agent_id != agent_id or row.user_id != user_id or not row.content.startswith('[[SOURCES:{"kind": "buildplan"'):
            return
        card = json.loads(row.content[len("[[SOURCES:"):-2])
        card["status"] = "started"
        row.content = chat_rows.card(**card)
        db.commit()
    finally:
        db.close()


# ── starting ─────────────────────────────────────────────────────────────

def _load(project_id: str, user_id: str, plan: str) -> dict:
    """The plan and the project it belongs to, or BuildError with what the person should hear."""
    from core.database import SessionLocal
    from core.models import DocumentRecord, Project
    from tools.documents import document_path, user_may_read

    db = SessionLocal()
    try:
        rec, project = db.get(DocumentRecord, plan), db.get(Project, project_id)
        path = document_path(plan)
        if rec is None or project is None or rec.project_id != project_id or path is None or path.suffix != ".md" or not path.is_file() or not user_may_read(user_id, plan, db=db):
            raise BuildError("That plan could not be found on this project.", 404)
        team = []  # what the specialists wrote (research, design, test cases): the plan is built from it, so Muse gets it too
        for doc in db.query(DocumentRecord).filter(DocumentRecord.project_id == project_id).order_by(DocumentRecord.created_at).all():
            doc_path = document_path(doc.filename)
            if (doc.filename == plan or doc.filename.endswith(("-run-guide.md",)) or (doc.title or "").lower().startswith(("first build plan", "next phase plan", "run guide", "qa report"))
                    or doc_path is None or doc_path.suffix != ".md" or not doc_path.is_file() or not user_may_read(user_id, doc.filename, db=db)):
                continue
            team.append((doc.title or doc.filename, doc_path.read_text(encoding="utf-8", errors="replace")[:30_000]))
        return {"plan": path.read_text(encoding="utf-8", errors="replace")[:80_000], "agent_id": rec.agent_id, "name": project.name, "goal": project.goal or "",
                "repo": project.repo or "", "team": team[:12]}
    finally:
        db.close()


def _need_sandbox() -> None:
    """Building runs generated code, so it only ever runs in a sandbox. Without one the person is told so, and nothing runs here."""
    try:
        import modal
    except ImportError as exc:
        raise BuildError("Building runs in a Modal sandbox, and the modal package is not installed on this server.", 503) from exc
    if not os.environ.get("META_API_KEY"):
        raise BuildError("Building needs the Muse key (META_API_KEY), which is not set on this server.", 503)
    if not (os.environ.get("MODAL_TOKEN_ID") or modal.config.config.get("token_id")):
        raise BuildError("Building runs in a Modal sandbox, and Modal is not set up on this server (MODAL_TOKEN_ID and MODAL_TOKEN_SECRET).", 503)


def latest_plan(project_id: str, user_id: str) -> str:
    """The file name of the newest first-build plan of this project that this person may read, or ''."""
    from core.database import SessionLocal
    from core.models import DocumentRecord
    from services.first_plan import DOC_PREFIX
    from tools.documents import user_may_read

    db = SessionLocal()
    try:
        rows = db.query(DocumentRecord).filter(DocumentRecord.project_id == project_id, DocumentRecord.title.ilike(f"{DOC_PREFIX}%")).order_by(DocumentRecord.created_at.desc()).all()
        return next((r.filename for r in rows if r.filename.endswith(".md") and user_may_read(user_id, r.filename, db=db)), "")
    finally:
        db.close()


def _refresh_goals(project_id: str, user_id: str) -> None:
    """In the background: the build changes what each bot is working towards. Never raises."""
    from services import goals
    runs.spawn(asyncio.to_thread(goals.refresh_project, project_id, user_id))


async def start(project_id: str, user_id: str, plan: str, row_id: int | None = None, agent_id: str = "") -> None:
    """The button was clicked, or a bot was asked to build. Checks what can be checked at once, puts the pill in the chat (the bot's, when one was asked),
    and builds in the background. BuildError: tell the person."""
    if (task := _running.get(project_id)) is not None and not task.done():
        raise BuildError("A build for this project is already running.", 409)
    _need_sandbox()
    info = await asyncio.to_thread(_load, project_id, user_id, plan)
    agent_id = agent_id or info["agent_id"]
    info["agent_id"] = agent_id
    pill = await asyncio.to_thread(chat_rows.write_row, agent_id, user_id, chat_rows.card("build", project=info["name"], status="running", stage="Starting"))
    if row_id:
        await asyncio.to_thread(_mark_started, agent_id, user_id, row_id)
    task = runs.spawn(_build_and_tell(project_id, user_id, info, pill))
    _running[project_id] = task
    task.add_done_callback(lambda t, k=project_id: _running.get(k) is t and _running.pop(k, None))
    from services import next_phase
    await next_phase.begin(project_id, user_id, agent_id, info["name"], info["goal"], plan)  # the team plans the next phase while this builds
    _refresh_goals(project_id, user_id)  # every bot's goal now includes the build under way


# ── the build ────────────────────────────────────────────────────────────

def empty_tarball() -> bytes:
    """An archive with one empty folder, for a project that has no repository: the sandbox unpacks every code archive the same way."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        info = tarfile.TarInfo("project")
        info.type = tarfile.DIRTYPE
        info.mode = 0o755
        tar.addfile(info)
    return buf.getvalue()


def _has_code(files: list[str]) -> bool:
    """Whether the checkout holds anything beyond what a new repository starts with, and what this module added."""
    return any(f.rsplit("/", 1)[-1].lower() not in _TRIVIAL and not f.startswith((".agents/", ".neuron/")) and f != "AGENTS.md" for f in files)


def _usable(guide: str | None) -> bool:
    return bool(guide) and len(guide.strip()) >= 200 and "```" in guide


async def _ticker(box: BuildBox, progress, skip: int = 0) -> None:
    """While Muse works, say how far it has got. It writes for a long time and says nothing meanwhile, so this counts the files."""
    started = time.monotonic()
    while True:
        await asyncio.sleep(TICK_SECONDS)
        try:
            count = max(0, len(await box.files()) - skip)  # skip: the starter's files, which Muse did not write
        except Exception:
            continue
        await progress(f"Muse is building · {count} file{'s' if count != 1 else ''} so far · {int((time.monotonic() - started) // 60)} min")


def team_files(team: list[tuple[str, str]]) -> dict[str, str]:
    """The specialists' documents as workspace files, with an index that says what each is."""
    if not team:
        return {}
    files = {f"{build_prompt.TEAM_DIR}/INDEX.md": "# Documents from the project team\n\n" + "\n".join(f"- `{i:02d}-{_slug(title)}.md`: {title}" for i, (title, _) in enumerate(team, 1)) + "\n"}
    files.update({f"{build_prompt.TEAM_DIR}/{i:02d}-{_slug(title)}.md": text for i, (title, text) in enumerate(team, 1)})
    return files


def _slug(title: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:50] or "document"


def parse_review(text: str) -> dict | None:
    """Muse's review as the schema describes it, or None when it did not answer in that shape."""
    import json

    try:
        data = json.loads(text[text.index("{"):text.rindex("}") + 1])
    except ValueError:
        return None
    ok = isinstance(data, dict) and isinstance(data.get("checks"), list) and isinstance(data.get("findings"), list)
    return data if ok else None


def serious(review: dict) -> list[dict]:
    """What must be fixed: blockers and majors, and every check that failed (a failed test case is a defect even when nobody wrote it up as one)."""
    found = [f for f in review["findings"] if isinstance(f, dict) and f.get("severity") in ("blocker", "major")]
    covered = " ".join(f"{f.get('title', '')} {f.get('why', '')}".lower() for f in found)
    for c in review["checks"]:
        if isinstance(c, dict) and c.get("status") == "fail" and str(c.get("item", "")).lower()[:40] not in covered:
            found.append({"file": "", "severity": "major", "title": f"Failed check: {c.get('item', '')}", "why": c.get("evidence", ""), "fix": "Make this check pass."})
    return found


def issues_text(found: list[dict]) -> str:
    return "\n".join(f"{i}. [{f.get('severity')}] {f.get('title')}" + (f" ({f['file']})" if f.get("file") else "") + f"\n   Why: {f.get('why')}\n   Fix: {f.get('fix')}" for i, f in enumerate(found, 1))


def qa_report(name: str, review: dict | None, rounds: int, fixed: int) -> str:
    """The review as a document a person can read: what was checked against what, with the evidence."""
    if review is None:
        return ""
    checks = [c for c in review["checks"] if isinstance(c, dict)]
    passed = sum(c.get("status") == "pass" for c in checks)
    lines = [f"# QA report: {name}", "", review.get("summary", ""), "",
             f"{passed} of {len(checks)} checks passed after {rounds} review{'s' if rounds != 1 else ''}; {fixed} problem{'s' if fixed != 1 else ''} found and fixed along the way.", "",
             "## Checks", "", "| Check | From | Result | Evidence |", "|---|---|---|---|"]
    for c in checks:
        cell = lambda v: str(v).replace("|", "/").replace("\n", " ")[:300]
        lines.append(f"| {cell(c.get('item'))} | {cell(c.get('source'))} | {cell(c.get('status'))} | {cell(c.get('evidence'))} |")
    left = [f for f in review["findings"] if isinstance(f, dict)]
    lines += ["", "## Still open", ""] + ([f"- [{f.get('severity')}] {f.get('title')}: {f.get('why')}" for f in left] or ["- Nothing."])
    return "\n".join(lines) + "\n"


async def review_and_fix(box: BuildBox, name: str, has_team: bool, progress) -> tuple[dict | None, int, int]:
    """A second, independent Muse run reviews the build against the plan and the team's documents, then Muse fixes what was verified wrong, up to MAX_REVIEWS times.
    Returns (the last review, reviews done, problems fixed). A review that cannot run never fails the build: the code is already good enough to hand over."""
    review, fixed, done = None, 0, 0
    for done in range(1, MAX_REVIEWS + 1):
        await progress(f"Reviewing the build · round {done}")
        try:
            review = parse_review(await box.inspect(build_prompt.review_prompt(name, has_team), build_prompt.REVIEW_SCHEMA))
        except RuntimeError:
            log.exception("The review of %s could not run", name)
            return review, done - 1, fixed
        found = serious(review) if review else []
        if review is None or not found or done == MAX_REVIEWS:
            break
        await progress(f"Fixing {len(found)} problem{'s' if len(found) != 1 else ''} the review found")
        await box.build(build_prompt.fix_prompt(name, issues_text(found)))
        fixed += len(found)
    return review, done, fixed


async def build(repo: str, user_id: str, plan: str, name: str, goal: str, progress, team: list[tuple[str, str]] | None = None, live=None) -> dict:
    """Muse builds the plan in a sandbox, a second Muse reviews it, and the first fixes what the review verified wrong. Returns
    {archive, files, guide, summary, review, qa}. Raises RuntimeError with something the person can read. `live` is the project's coding session
    (services/preview.py): the build then runs in its sandbox, where the app is started as soon as it can be and goes on as the session afterwards."""
    token, tarball = "", b""
    if repo:
        from services.wiki import head_sha

        token = await cr._token(repo, user_id)
        tarball = await cr.fetch_tarball(repo, await head_sha(repo, token), token)
    team = team or []

    async def steps(box) -> dict:
        await progress("Setting up the sandbox")
        await box.load(tarball or empty_tarball(), {**build_prompt.skill_files(), build_prompt.PLAN_PATH: plan, **team_files(team), **imagegen.files()})
        present = await box.files()
        has_code = _has_code(present)
        seeded = not has_code  # a new project (no repository, or one with only a README): the starter is up and hot-reloading within seconds, before Muse has written anything
        if seeded:
            for path, text in starter.files(name).items():
                if path not in present:
                    await box.write(path, text)
            await box.install_starter()
        own_rules = await box.read("AGENTS.md")  # the project's own, if it has one: the build rules are added to it, not put in its place
        await box.write("AGENTS.md", build_prompt.with_rules(own_rules))
        await progress("Muse is building")
        ticker = asyncio.create_task(_ticker(box, progress, len(starter.files(name)) if seeded else 0))
        try:
            summary = await box.build(build_prompt.build_prompt(name, goal, has_code, bool(team), seeded))
        finally:
            ticker.cancel()
        guide = await box.read(build_prompt.RUN_GUIDE)
        if not _usable(guide):  # a build with no usable run guide is half a build: ask for the guide, from commands that are run
            await progress("Writing the run guide")
            await box.build(build_prompt.run_guide_prompt(name))
        review, rounds, fixed = await review_and_fix(box, name, bool(team), progress)
        guide = await box.read(build_prompt.RUN_GUIDE)
        await box.write("AGENTS.md", build_prompt.without_rules(await box.read("AGENTS.md"), own_rules is not None))
        await progress("Packaging the code")
        files = await box.files()
        archive = await box.export()
        return {"archive": archive, "files": files, "guide": (guide or "").strip(), "summary": summary.strip()[-3000:], "review": review,
                "qa": qa_report(name, review, rounds, fixed), "team": len(team)}

    if live is not None:
        from services import preview

        return await preview.run_live(live, steps)
    async with BuildBox(EFFORT, TIMEOUT) as box:
        return await steps(box)


# ── what comes of it ─────────────────────────────────────────────────────

def _save(project_id: str, user_id: str, agent_id: str, name: str, archive: bytes, guide: str, qa: str = "") -> dict:
    """The code as a zip and the run guide as a markdown file, in Docs. Returns their names."""
    from core.usage import reset_usage_user, set_usage_user
    from tools.documents import made_message, record, save_new

    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:40] or "project"
    acting = set_usage_user(user_id, project_id)  # the documents are this person's
    try:
        zip_name = save_new(f"{slug}-first-build", "zip", lambda path: path.write_bytes(archive))
        record(agent_id, made_message(name, "zip", zip_name), f"{name}: first build (code)", "Code archive")
        guide_name = ""
        if guide:
            guide_name = save_new(f"{slug}-run-guide", "md", lambda path: path.write_text(guide + "\n", encoding="utf-8"))
            record(agent_id, made_message(name, "md", guide_name), f"Run guide: {name}", guide)
        qa_name = ""
        if qa:
            qa_name = save_new(f"{slug}-qa-report", "md", lambda path: path.write_text(qa, encoding="utf-8"))
            record(agent_id, made_message(name, "md", qa_name), f"QA report: {name}", qa)
        return {"zip": zip_name, "guide": guide_name, "qa": qa_name}
    finally:
        reset_usage_user(acting)


def _offer_pull_request(project_id: str, user_id: str, agent_id: str, repo: str, name: str, saved: dict, files: int) -> None:
    """Park the pull request behind the Allow/Deny card: pushing a branch to the repository is an outward write, so a person says yes first."""
    from core.database import SessionLocal
    from core.usage import reset_usage_user, set_usage_user
    from tools.outbox import _park

    acting = set_usage_user(user_id, project_id)
    db = SessionLocal()
    try:
        call = {"tool": "publish_build", "args": {"archive": saved["zip"], "guide": saved["guide"]}, "jira_key": "", "repo": repo}
        detail = (f"A new branch of {repo} with the first build of {name}: {files} files, the same code as the saved archive, and a pull request that carries the run guide. "
                  "Nothing on the default branch changes until someone merges it.")
        _park(agent_id, f"Open a pull request with the first build of {name} on {repo}", detail, call, db, interactive=True, title=f"Approve: pull request for {name}'s first build")
    finally:
        db.close()
        reset_usage_user(acting)


def handover(name: str, result: dict | None, failure: str, saved: dict | None, repo: str) -> str:
    """What the Master is told when the build ends, so that it tells the person in its own words, with the exact commands."""
    from services.review_render import untrusted_block

    if result is None:
        return (f"The build of the first version of {name} did not finish: {failure or 'unknown error'}.\n"
                "Tell the person in two or three casual sentences what went wrong and what they can do about it (they can press the build button again). No headings.")
    guide = result["guide"]
    body = (f"Muse built the first version of {name} from the approved plan, in a sandbox: {len(result['files'])} files. The code is saved in Docs as a zip"
            + (", and the run guide is saved too" if saved and saved["guide"] else "")
            + (f"; a second Muse reviewed it against the plan and {result['team']} team document(s), and the QA report is saved too" if saved and saved.get("qa") else "")
            + (f". A pull request on {repo} is waiting for their Allow in this chat." if repo else ".") + "\n\n")
    left = [f for f in (result.get("review") or {}).get("findings", []) if isinstance(f, dict)]
    if result.get("session"):
        body += "A live coding session has been started on it: the app is starting beside this chat. Tell them to say what they want changed, and that you will make each change in the running app.\n\n"
    if left:
        body += "The review left these open; say them plainly, short:\n" + "\n".join(f"- [{f.get('severity')}] {f.get('title')}" for f in left[:8]) + "\n\n"
    if guide:
        return (body + "Give the person the run guide now: one or two sentences on what was built, then the exact commands from the guide, in order, each in its own code block, "
                "copied as written, with the address to open. Say what the guide lists as not verified. Do not invent commands that are not in it.\n"
                + untrusted_block("run guide", guide[:12000]))
    return body + ("Muse did not leave a run guide, so there are no verified commands to give. Say so plainly, say what Muse reported, and do not make commands up:\n"
                   + untrusted_block("Muse's closing message", result["summary"]))


async def _build_and_tell(project_id: str, user_id: str, info: dict, pill: int) -> None:
    from routes.chat import _start_kickoff

    name, repo, agent_id = info["name"], info["repo"], info["agent_id"]
    last = [""]

    async def progress(stage: str) -> None:
        if stage != last[0]:
            last[0] = stage
            await asyncio.to_thread(chat_rows.write_row, agent_id, user_id, chat_rows.card("build", project=name, status="running", stage=stage), pill)

    result, saved, failure, live = None, None, "", None
    log.info("Build of %s started for user %s", name, user_id)
    try:
        if _slots.locked():
            await progress("Waiting for another build to finish")
        async with _slots:
            try:  # the project's coding session is there from the start: the app comes up beside the chat while it is still being written
                from services import preview
                live = await preview.begin_live(project_id, user_id, agent_id, name)
            except Exception:
                log.exception("Could not start the live session for %s: the build goes on without it", name)
            result = await build(repo, user_id, info["plan"], name, info["goal"], progress, info.get("team"), live)
            if live is not None:
                result["session"] = live.status in ("starting", "running")
        saved = await asyncio.to_thread(_save, project_id, user_id, agent_id, name, result["archive"], result["guide"], result["qa"])
        log.info("Build of %s finished: %s files, guide=%s", name, len(result["files"]), bool(result["guide"]))
    except asyncio.CancelledError:
        await asyncio.to_thread(chat_rows.write_row, agent_id, user_id, chat_rows.card("build", project=name, status="failed", reason="Stopped: the server was restarting."), pill)
        raise
    except Exception as exc:
        log.exception("Build of %s failed", name)
        failure = str(exc) if isinstance(exc, (RuntimeError, ValueError)) else type(exc).__name__
        result = saved = None
        if live is not None:
            from services import preview
            await preview.abandon(live, failure)
    if result is not None and saved is not None:
        done = chat_rows.card("build", project=name, status="done", files=len(result["files"]), guide=bool(result["guide"]), repo=repo, archive=saved["zip"], project_id=project_id)
        await asyncio.to_thread(chat_rows.write_row, agent_id, user_id, done, pill)
        from tools.documents import document_url
        await asyncio.to_thread(chat_rows.write_row, agent_id, user_id, f"[{name}: first build (code)]({document_url(saved['zip'])})")
        if saved["guide"]:
            await asyncio.to_thread(chat_rows.write_row, agent_id, user_id, f"[Run guide: {name}]({document_url(saved['guide'])})")
        if saved["qa"]:
            await asyncio.to_thread(chat_rows.write_row, agent_id, user_id, f"[QA report: {name}]({document_url(saved['qa'])})")
        if live is None:  # no session was up during the build: the code is built, so it comes up now, before there is a pull request to review
            try:
                from services import preview
                await preview.start_session(project_id, name, user_id, agent_id, saved["zip"])
                result["session"] = True
            except Exception:
                log.exception("Could not start the coding session for %s", name)
        if repo:
            try:
                await asyncio.to_thread(_offer_pull_request, project_id, user_id, agent_id, repo, name, saved, len(result["files"]))
            except Exception:
                log.exception("Could not offer the pull request for %s", name)
    else:
        await asyncio.to_thread(chat_rows.write_row, agent_id, user_id, chat_rows.card("build", project=name, status="failed", reason=failure[:300]), pill)

    _start_kickoff(agent_id, "Neuron", project_id, handover(name, result, failure, saved, repo), user_id, no_tools=True)
    _refresh_goals(project_id, user_id)  # and again with what came of it


async def start_for_bot(agent_id: str, plan: str = "") -> str:
    """The build_project tool: the person asked a bot to build. Returns what the bot is told."""
    from core.database import SessionLocal
    from core.models import Agent
    from core.usage import current_user

    user = current_user()
    db = SessionLocal()
    try:
        agent = db.get(Agent, agent_id)
        project_id = agent.project_id if agent else ""
    finally:
        db.close()
    if not user or not project_id:
        return "Error: building needs a signed-in person on a project."
    name = (plan or "").strip() or await asyncio.to_thread(latest_plan, project_id, user)
    if not name:
        return "Error: there is no first-build plan on this project yet. Write it first (create_document, titled \"First build plan: ...\"), then build."
    try:
        await start(project_id, user, name, None, agent_id)
    except BuildError as exc:
        return f"Error: {exc}"
    return ("Started: Muse is building the first version from the plan in a sandbox. It takes several minutes; the person is shown progress in this chat and is told "
            "when it is done, with the run guide. Tell them it has started. Do not call build_project again and do not start building by other means.")
