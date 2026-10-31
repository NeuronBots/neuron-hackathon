"""The project setup asks the model for themes and teams. An answer with nothing in it must not fail the step."""
import asyncio

import pytest

from services import project_setup as ps

THEMES = '{"themes":[{"title":"Names Meaning Light","detail":"Fits Lumen.","examples":["Lux","Ray"]}]}'


def run(coro):
    return asyncio.run(coro)


def fake(monkeypatch, answers):
    calls = []

    def complete(model, system, user, max_tokens=1024, **kw):
        calls.append({"max_tokens": max_tokens, **kw})
        reply = answers[min(len(calls), len(answers)) - 1]
        if isinstance(reply, Exception):
            raise reply
        return reply
    monkeypatch.setattr("llm.client.complete", complete)
    return calls


def test_the_calls_do_not_ask_the_model_to_think_which_is_how_it_once_returned_nothing(monkeypatch):
    calls = fake(monkeypatch, [THEMES])
    themes = run(ps.suggest_themes("Lumen", "A health app", ""))
    assert [t["title"] for t in themes] == ["Names Meaning Light"]
    assert calls == [{"max_tokens": 2500}] and "thinking_budget" not in calls[0]


def test_an_empty_answer_is_asked_again_once_with_twice_the_room(monkeypatch):
    calls = fake(monkeypatch, ["", THEMES])
    assert run(ps.suggest_themes("Lumen", "A health app", ""))[0]["title"] == "Names Meaning Light"
    assert [c["max_tokens"] for c in calls] == [2500, 5000]


def test_a_second_empty_answer_still_fails_with_the_same_message(monkeypatch):
    calls = fake(monkeypatch, ["", "   "])
    with pytest.raises(RuntimeError, match="The reply could not be read"):
        run(ps.suggest_themes("Lumen", "A health app", ""))
    assert len(calls) == 2  # it does not keep trying


def test_an_answer_that_is_not_json_is_not_retried_since_more_room_would_not_help(monkeypatch):
    calls = fake(monkeypatch, ["I could not think of any themes, sorry."])
    with pytest.raises(RuntimeError, match="The reply could not be read"):
        run(ps.suggest_themes("Lumen", "A health app", ""))
    assert len(calls) == 1


def test_a_failed_call_is_not_swallowed(monkeypatch):
    fake(monkeypatch, [RuntimeError("upstream 500")])
    with pytest.raises(RuntimeError, match="upstream 500"):
        run(ps.suggest_themes("Lumen", "A health app", ""))


def test_the_team_step_asks_the_same_way(monkeypatch):
    calls = fake(monkeypatch, ['{"agents":[{"name":"Lux","label":"QA Architect","field":"testing","system_prompt":"Lux tests."}]}'])
    agents = run(ps.suggest_architects("Lumen", "A health app", "", "Names Meaning Light", ["Lux"]))
    assert agents[0]["name"] == "Lux" and calls == [{"max_tokens": 4000}]
