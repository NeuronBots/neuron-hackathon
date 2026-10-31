"""Naming a bot made from a request, and never mistaking the system's own messages for a request to make one."""
import asyncio
from pathlib import Path

import pytest

from agents import naming
from agents.naming import asks_for_new_bot, clean_name, name_from_job, suggest_name
from services import first_plan


def test_the_first_plan_brief_is_not_a_request_for_a_bot():
    """It once was: the Master took it for 'create a bot', proposed a bot called 'Project Was Just' and gave it the brief as its role."""
    brief = first_plan.brief("Lumen", "A health app for nurses", "acme/lumen", "Flask and React.", [("Ada", "QA Architect", "testing")], "Tanay")
    assert asks_for_new_bot(brief) is False
    assert asks_for_new_bot(first_plan.brief("x", "", "", "", [], "")) is False


def test_the_chat_never_reads_an_automatic_message_as_a_request_for_a_bot():
    """A kickoff is the system talking to the bot. This keeps that guard from being lost; running a whole turn needs a model."""
    src = (Path(__file__).resolve().parents[1] / "routes/chat.py").read_text()
    assert src.count('agent.is_master == "true" and not kickoff and asks_for_new_bot(body.content)') == 1


@pytest.mark.parametrize("text", [
    "Create a bot for marketing and investor research",
    "Lets create a bot that helps in financial management of the company",
    "Please set up an agent to watch my pull requests",
    "make me a bot for support tickets",
])
def test_real_requests_for_a_bot_are_still_recognised(text):
    assert asks_for_new_bot(text) is True


@pytest.mark.parametrize("text,expected", [
    ("Lets create a bot that helps in financial management of the company", "Financial Management Company"),
    ("A new project was just created by Tanay. You lead it.", "Created Tanay"),
])
def test_the_fallback_name_skips_filler_words(text, expected):
    assert name_from_job(text) == expected
    assert "Lets" not in name_from_job(text) and "Helps" not in name_from_job(text) and "Just" not in name_from_job(text)


@pytest.mark.parametrize("raw,name", [
    ("Mint", "Mint"), ('"Finance Scribe"', "Finance Scribe"), ("  catalyst  ", "Catalyst"), ("Ledger Keeper\nA bot that keeps the books", "Ledger Keeper"),
    ("Financial Management Assistant Bot", ""), ("", ""), ("a b c d e", ""), ("The finance bot", ""), ("X" * 60, ""), ("12345", ""), ("Name: Mint", ""),
])
def test_a_models_answer_is_a_name_only_when_it_looks_like_one(raw, name):
    assert clean_name(raw) == name


def run(coro):
    return asyncio.run(coro)


def test_the_name_comes_from_the_model_when_it_gives_a_good_one(monkeypatch):
    seen = []

    def fake(model, system, user, max_tokens=1024, **kw):
        seen.append((model, user, max_tokens))
        return "Ledger"
    monkeypatch.setattr("llm.client.complete", fake)
    assert run(suggest_name("Lets create a bot that helps in financial management")) == "Ledger"
    assert seen[0][1] == "Lets create a bot that helps in financial management" and seen[0][2] == 30  # a small, cheap call


@pytest.mark.parametrize("failure", ["boom", "bad answer"])
def test_a_failed_or_odd_answer_falls_back_to_a_name_from_the_words(monkeypatch, failure):
    def fake(model, system, user, max_tokens=1024, **kw):
        if failure == "boom":
            raise RuntimeError("no key")
        return "This is a long sentence that is not a name at all"
    monkeypatch.setattr("llm.client.complete", fake)
    assert run(suggest_name("Create a bot for marketing and investor research")) == name_from_job("Create a bot for marketing and investor research") == "Marketing Investor Research"


def test_a_slow_model_does_not_hold_up_the_chat(monkeypatch):
    import time

    def slow(*a, **k):
        time.sleep(0.5)
        return "Late"
    monkeypatch.setattr("llm.client.complete", slow)
    monkeypatch.setattr(asyncio, "wait_for", lambda coro, timeout: _timeout(coro))
    assert run(suggest_name("Create a bot for support")) == name_from_job("Create a bot for support")


async def _timeout(coro):
    coro.close() if hasattr(coro, "close") else None
    raise asyncio.TimeoutError
