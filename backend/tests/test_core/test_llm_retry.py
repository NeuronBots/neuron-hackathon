"""post_retry waits and asks again while the provider is busy, and gives the last answer after four tries."""
import asyncio

from llm import client


class Resp:
    def __init__(self, status_code):
        self.status_code = status_code


class Fake:
    def __init__(self, *codes):
        self.codes, self.calls = list(codes), 0

    def post(self, *a, **kw):
        self.calls += 1
        return Resp(self.codes.pop(0))


class AsyncFake(Fake):
    async def post(self, *a, **kw):
        return super().post()


def test_busy_then_ok(monkeypatch):
    slept = []
    monkeypatch.setattr(client.time, "sleep", slept.append)
    fake = Fake(429, 503, 200)
    assert client.post_retry(fake, {}).status_code == 200 and fake.calls == 3 and slept == [1.5, 3.0]


def test_gives_up_after_four_tries_and_does_not_retry_other_errors(monkeypatch):
    monkeypatch.setattr(client.time, "sleep", lambda s: None)
    busy = Fake(429, 429, 429, 529)
    assert client.post_retry(busy, {}).status_code == 529 and busy.calls == 4
    bad = Fake(400)
    assert client.post_retry(bad, {}).status_code == 400 and bad.calls == 1


def test_async_version(monkeypatch):
    async def no_sleep(s):
        pass

    monkeypatch.setattr(client.asyncio, "sleep", no_sleep)
    fake = AsyncFake(502, 200)
    assert asyncio.run(client.apost_retry(fake, {})).status_code == 200 and fake.calls == 2


def test_complete_stops_at_the_token_cap(monkeypatch):
    monkeypatch.setattr(client, "ANTHROPIC_API_KEY", "k")
    monkeypatch.setattr(client, "over_limit", lambda uid: "You have used this week's token limit.")
    try:
        client.complete("workhorse", "s", "u")
    except RuntimeError as exc:
        assert "token limit" in str(exc)
    else:
        raise AssertionError("complete() ran past the cap")


def test_complete_without_a_user_is_not_capped(monkeypatch):
    class Reached(Exception):
        pass

    def reach(*a, **kw):
        raise Reached

    monkeypatch.setattr(client, "ANTHROPIC_API_KEY", "k")
    monkeypatch.setattr(client, "post_retry", reach)  # the real over_limit runs, with no usage user
    try:
        client.complete("workhorse", "s", "u")
    except Reached:
        pass
    else:
        raise AssertionError("complete() did not reach the model")


def test_continue_text_retries_a_busy_provider(monkeypatch):
    monkeypatch.setattr(client.time, "sleep", lambda s: None)
    answers = [Resp(429), Resp(200)]
    answers[1].json = lambda: {"content": [{"type": "text", "text": " the rest"}], "usage": {}}

    class Http:
        def __init__(self, *a, **kw):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def post(self, *a, **kw):
            return answers.pop(0)

    monkeypatch.setattr(client.httpx, "Client", Http)
    assert client.continue_text("workhorse", "s", "p", "half").strip() == "the rest" and not answers


def test_the_sender_followup_gives_a_notice_not_an_answer_when_over_the_cap(monkeypatch):
    from agents import followup

    monkeypatch.setattr(followup, "_followup_request", lambda *a: ("s", "p"))
    monkeypatch.setattr(followup, "over_limit", lambda uid: "You have used this week's token limit.")

    async def run():
        return [item async for item in followup.stream_sender_followup(None, [], "hi", None)]

    assert asyncio.run(run()) == [("notice", "You have used this week's token limit."), ("final", None)]


def test_peer_summary_is_empty_over_the_cap(monkeypatch):
    from agents import followup

    monkeypatch.setattr(followup, "over_limit", lambda uid: "capped")
    bot = type("Bot", (), {"name": "A"})()
    assert followup._peer_summary(bot, bot, "q", "r", None) == ""


def test_the_fallback_makes_one_try(monkeypatch):
    async def no_sleep(s):
        pass

    monkeypatch.setattr(client.asyncio, "sleep", no_sleep)
    fake = AsyncFake(429, 429)
    assert asyncio.run(client.apost_retry(fake, {}, tries=1)).status_code == 429 and fake.calls == 1


def test_a_dropped_connection_is_retried_then_raised(monkeypatch):
    monkeypatch.setattr(client.time, "sleep", lambda s: None)

    class Flaky(Fake):
        def post(self, *a, **kw):
            self.calls += 1
            if self.calls < 3:
                raise client.httpx.ReadTimeout("slow")
            return Resp(200)

    flaky = Flaky()
    assert client.post_retry(flaky, {}).status_code == 200 and flaky.calls == 3

    class Dead(Fake):
        def post(self, *a, **kw):
            self.calls += 1
            raise client.httpx.ConnectTimeout("down")

    dead = Dead()
    try:
        client.post_retry(dead, {})
    except client.httpx.ConnectTimeout:
        assert dead.calls == client.TRIES
    else:
        raise AssertionError("the last timeout should surface")
