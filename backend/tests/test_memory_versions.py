"""A bot's core memory changes wait as drafts; notes and people's answers go live at once; any version can be rolled back to."""
from agents import versions
from agents.memory import add_core_memory, propose_core_memory
from core.models import Agent, MemoryVersion, Project


def _bot(db):
    db.add(Project(id="p1", name="P"))
    db.commit()
    bot = Agent(id="a1", name="Bot", project_id="p1", system_prompt="x", core_memory="- likes tea")
    db.add(bot)
    db.commit()
    return bot


def test_own_remember_is_a_draft_until_promoted(db):
    bot = _bot(db)
    assert propose_core_memory(bot, "ships on Friday") and propose_core_memory(bot, "prefers email")
    db.commit()
    assert bot.core_memory == "- likes tea"  # the prompt still reads the prod text
    draft = versions.draft_of(bot, "core_memory")
    assert draft.content == "- likes tea\n- ships on Friday\n- prefers email"  # two wishes, one pending change
    versions.promote(bot, draft)
    db.commit()
    assert "prefers email" in bot.core_memory and versions.draft_of(bot, "core_memory") is None


def test_reject_and_rollback(db):
    bot = _bot(db)
    propose_core_memory(bot, "wrong idea")
    versions.reject(versions.draft_of(bot, "core_memory"))
    db.commit()
    assert bot.core_memory == "- likes tea"
    add_core_memory(bot, "Ann answered: yes")  # a person's answer is live at once
    db.commit()
    assert "Ann answered" in bot.core_memory
    first = db.query(MemoryVersion).filter_by(agent_id="a1", source="before versions").one()
    versions.promote(bot, first)  # roll back
    db.commit()
    assert bot.core_memory == "- likes tea" and db.query(MemoryVersion).filter_by(is_prod=True).count() == 1


def test_memory_notes_are_promoted_at_once(db):
    bot = _bot(db)
    versions.set_prod(bot, "memory", "folded notes", "older turns folded in")
    db.commit()
    assert bot.memory == "folded notes" and versions.draft_of(bot, "memory") is None
