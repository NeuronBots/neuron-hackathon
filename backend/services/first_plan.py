"""A new project's first piece of work: the team plans the first build.

Right after a project is created its Master is started on a board task. It reads what there is (the goal, the repository), asks its teammates for
their specialist input, takes the few decisions that matter to the person through Decisions pending (with a recommendation each), and then writes
ONE detailed plan as a document that the first code can be built from. The plan is a document, not code: nothing is built until the person has read it.

It uses only what bots already have (the board, request_decision, ask_teammate, create_document); anything it does outside the app (Jira, GitHub
writes) still waits for approval like any other outward action."""
from __future__ import annotations

import os
from pathlib import Path

from sqlalchemy.orm import Session

from core.models import Agent, DocumentRecord, Project, Task

DOC_PREFIX = "First build plan"  # the plan document's title starts with this; a button under it starts the build (services/first_build.py)
TITLE = "Draft the first-build plan"  # the board task's title; decisions about the plan use it too, so they stay attached to it


FLOWS_DIR = Path(__file__).resolve().parents[1] / "flows"  # the instructions live in files, read at every run: edit them and run again, no restart
RESEARCH_MODES = ("full", "light", "reuse", "off")
_COUNT = ("zero", "one", "two", "three", "four", "five")


class _Keep(dict):
    def __missing__(self, key: str) -> str:
        return "{" + key + "}"  # an unknown placeholder is left as written


def _fill(file: str, **values: object) -> str:
    return (FLOWS_DIR / file).read_text(encoding="utf-8").format_map(_Keep(values))


def defaults() -> tuple[str, int]:
    """How much the planning spends when nothing says otherwise: PLAN_RESEARCH (full, light, reuse or off) and PLAN_MAX_DECISIONS (0 to 5)."""
    mode = os.getenv("PLAN_RESEARCH", "full").strip().lower()
    try:
        asks = int(os.getenv("PLAN_MAX_DECISIONS", "5"))
    except ValueError:
        asks = 5
    return (mode if mode in RESEARCH_MODES else "full"), max(0, min(5, asks))


def research_docs(db: Session, project_id: str) -> list[tuple[str, str]]:
    """(title, file) of what the team has already saved for this project: what a later planning run can reuse instead of researching again."""
    skip = (DOC_PREFIX.lower(), "next phase plan", "run guide", "qa report")
    rows = db.query(DocumentRecord).filter(DocumentRecord.project_id == project_id).order_by(DocumentRecord.created_at).all()
    return [(r.title or r.filename, r.filename) for r in rows if r.filename.endswith(".md") and not (r.title or "").lower().startswith(skip)][:12]


def brief(name: str, goal: str, repo: str, summary: str, team: list[tuple[str, str, str]], person: str,
          research: str | None = None, decisions: int | None = None, docs: list[tuple[str, str]] | None = None) -> str:
    """What the Master is told when the project is created, or when planning is run again. `research` and `decisions` set how much it spends."""
    mode, asks = defaults()
    mode = research if research in RESEARCH_MODES else mode
    asks = asks if decisions is None else max(0, min(5, decisions))
    mates = "\n".join(f"- {n}" + (f" ({', '.join(x for x in (label, field) if x)})" if label or field else "") for n, label, field in team) or "- (no teammates yet: do their parts yourself)"
    if mode == "reuse" and not docs:
        mode = "light"  # nothing saved to reuse: a short pass from what the team knows
    step3 = _fill(f"plan.research.{mode}.md", title=TITLE, docs="\n".join(f"- {t} (file {f})" for t, f in docs or []))
    step4 = _fill("plan.decisions.none.md" if asks == 0 else "plan.decisions.md", title=TITLE, max_decisions=_COUNT[asks])
    return _fill(
        "plan.md", name=name, goal=goal or "(not given: ask for it as your first decision)", person=person or "the owner", mates=mates,
        repo_lines=(f"Repository: {repo}\n" if repo else "Repository: none linked yet\n") + (f"What is in the repository: {summary}\n" if summary else ""),
        repo_read=", then look at the repository with your GitHub tools (README, the folder layout, the manifests) so the plan fits what exists; do not guess what is there" if repo else "",
        title=TITLE, doc_prefix=DOC_PREFIX, research=step3, decisions=step4,
    )


def queue(db: Session, project: Project, master: Agent, team: list[tuple[str, str, str]], person: str) -> str:
    """Put the planning on the Master's board, so it is visible and is picked up again if the server restarts before it begins. Returns the brief."""
    db.add(Task(agent_id=master.id, title=TITLE, status="working"))
    return brief(project.name, project.goal or "", project.repo or "", project.summary or "", team, person, docs=research_docs(db, project.id))


async def start(master_id: str, project_id: str, user_id: str, text: str) -> None:
    """Start the Master on the brief, in the background (it runs for as long as the planning takes, and the person is told in the project's chat)."""
    import asyncio

    from routes.chat import _start_kickoff
    from services import chat_rows

    await asyncio.to_thread(chat_rows.phase, master_id, user_id, "plan", "The team researches, asks you what matters, and writes the first-build plan.")
    _start_kickoff(master_id, "Neuron", project_id, text, user_id)
