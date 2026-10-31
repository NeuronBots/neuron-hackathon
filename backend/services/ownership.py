"""The plan's "Who owns what" section, taken to each bot as a line of its core memory (a draft the person promotes), so every bot knows its part of the finished app."""
import re

from sqlalchemy.orm import Session

from core.models import Agent

_HEAD = re.compile(r"^#{1,6}\s*(?:\d+\.\s*)?who owns what\b.*$", re.I | re.M)
_LINE = re.compile(r"^\s*[-*]\s*\**([^:*\n]{1,60}?)\**\s*:\s*(.+)$")


def parse(plan: str) -> dict[str, str]:
    """{name: what they own} from the plan's "Who owns what" section, up to the next heading."""
    found = _HEAD.search(plan or "")
    if not found:
        return {}
    section = re.split(r"^#{1,6}\s", plan[found.end():], maxsplit=1, flags=re.M)[0]
    return {m[1].strip(): " ".join(m[2].split())[:300] for line in section.splitlines() if (m := _LINE.match(line))}


def assign(project_id: str, project_name: str, plan: str, db: Session) -> int:
    """Stage each named bot's line as a core-memory draft. Returns how many bots got one."""
    from agents.memory import propose_core_memory
    from tools.outbox import notify_pending

    owns = {name.lower(): text for name, text in parse(plan).items()}
    staged = []
    for bot in db.query(Agent).filter(Agent.project_id == project_id):
        if (text := owns.get(bot.name.lower())) and propose_core_memory(bot, f"In {project_name} you own: {text}"):
            staged.append(bot)
    db.commit()
    if staged:
        notify_pending(staged[0].id, "Review what each bot owns in the app", db)
    return len(staged)
