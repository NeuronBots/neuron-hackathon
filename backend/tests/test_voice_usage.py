"""Tests for ElevenLabs voice usage accounting and quota enforcement."""
from datetime import datetime, timedelta
import pytest
from starlette.testclient import TestClient

from core.access import COOKIE, hash_password
import core.database as database
from core.limits import over_limit, tokens_used, voice_units_used, week_start
from core.models import Agent, AuthSession, Project, ProjectMember, UsageEvent, User
from core.usage import (
    StreamedSynthesisAccumulator,
    record_stt_usage,
    record_tts_usage,
    settle_streamed_synthesis,
)
from routes.auth import PLAN_CAPS


@pytest.fixture
def client(db):
    from router import app
    app.dependency_overrides[database.get_db] = lambda: db
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


def make_user(db, name: str, plan: str = "free", admin: bool = False, token: str | None = None) -> User:
    user = User(
        id=f"user_{name}",
        email=f"{name}@example.com",
        name=name,
        password_hash=hash_password("testpass123"),
        plan=plan,
        is_admin=admin,
    )
    db.add(user)
    db.commit()
    if token:
        db.add(AuthSession(token=token, user_id=user.id, expires_at=datetime.utcnow() + timedelta(days=1)))
        db.commit()
    return user


def test_tts_call_completes_and_settles_to_central_ledger(db):
    """Given an ElevenLabs TTS call completes, when the response settles,
    then a usage event with characters, model/voice ID, audio seconds,
    user/agent ID and request ID is written to the central ledger."""
    user = make_user(db, "tts_user")
    proj = Project(id="proj_1", name="Voice Project")
    db.add(proj)
    db.commit()
    agent = Agent(id="agent_voice_1", name="VoiceAgent", system_prompt="voice", project_id="proj_1")
    db.add(agent)
    db.commit()

    event = record_tts_usage(
        user_id=user.id,
        characters=350,
        voice_id="EXAVITQu4vr4xnSDxMaL",
        model="eleven_multilingual_v2",
        audio_seconds=8.5,
        agent_id=agent.id,
        request_id="req_tts_001",
        project_id="proj_1",
        db=db,
    )

    assert event is not None
    assert event.user_id == user.id
    assert event.agent_id == agent.id
    assert event.voice_id == "EXAVITQu4vr4xnSDxMaL"
    assert event.model == "eleven_multilingual_v2"
    assert event.characters == 350
    assert event.audio_seconds == 8.5
    assert event.request_id == "req_tts_001"
    assert event.kind == "tts"

    # Verify queryable in database
    row = db.get(UsageEvent, event.id)
    assert row is not None
    assert row.characters == 350
    assert row.audio_seconds == 8.5


def test_tts_enforces_elevenlabs_minimum_characters(db):
    """ElevenLabs bills with minimum characters per request (e.g. 250 chars)."""
    user = make_user(db, "min_char_user")
    event = record_tts_usage(
        user_id=user.id,
        characters=45,  # Short utterance under minimum
        voice_id="voice_short",
        model="eleven_multilingual_v2",
        audio_seconds=1.2,
        request_id="req_tts_min",
        apply_minimum=True,
        min_characters=250,
        db=db,
    )
    assert event is not None
    assert event.characters == 250  # Billed at the 250 minimum


def test_stt_call_settles_and_dedupes_on_retry(db):
    """Given an ElevenLabs STT call completes, when the response settles,
    then minutes/characters and request ID are logged with dedupe on retry."""
    user = make_user(db, "stt_user")
    req_id = "stt_retry_req_42"

    # First STT settlement
    event1 = record_stt_usage(
        user_id=user.id,
        audio_seconds=90.0,
        minutes=1.5,
        characters=520,
        model="scribe_v1",
        agent_id="agent_scribe",
        request_id=req_id,
        db=db,
    )
    assert event1 is not None
    assert event1.characters == 520
    assert event1.audio_seconds == 90.0
    assert event1.request_id == req_id
    assert event1.kind == "stt"

    initial_count = db.query(UsageEvent).filter(UsageEvent.request_id == req_id).count()
    assert initial_count == 1

    # Retry with the same request_id must dedupe and not add another row
    event2 = record_stt_usage(
        user_id=user.id,
        audio_seconds=90.0,
        minutes=1.5,
        characters=520,
        model="scribe_v1",
        agent_id="agent_scribe",
        request_id=req_id,
        db=db,
    )
    assert event2 is not None
    assert event2.id == event1.id

    after_count = db.query(UsageEvent).filter(UsageEvent.request_id == req_id).count()
    assert after_count == 1


def test_streamed_synthesis_partials_and_single_settlement(db):
    """Given streamed synthesis with partials/retries, when events emit,
    then only final settled usage is counted once."""
    user = make_user(db, "stream_user")
    req_id = "stream_sess_999"

    acc = StreamedSynthesisAccumulator(
        request_id=req_id,
        user_id=user.id,
        agent_id="agent_stream",
        voice_id="cgSgspJ2msm6clMCkdW9",
        model="eleven_turbo_v2_5",
    )

    # Partial chunk events emit during stream
    acc.emit_partial(characters=50, audio_seconds=1.0)
    acc.emit_partial(characters=60, audio_seconds=1.2)
    acc.emit_partial(characters=70, audio_seconds=1.5)

    # While streaming, nothing is committed to the central ledger yet
    assert db.query(UsageEvent).filter(UsageEvent.request_id == req_id).count() == 0

    # Stream settles
    settled = acc.settle(min_characters=0, db=db)
    assert settled is not None
    assert settled.characters == 180  # 50 + 60 + 70
    assert pytest.approx(settled.audio_seconds, 0.01) == 3.7
    assert settled.request_id == req_id

    assert db.query(UsageEvent).filter(UsageEvent.request_id == req_id).count() == 1

    # Retry or duplicate settle call on the same stream session
    settled_retry = acc.settle(min_characters=0, db=db)
    assert settled_retry.id == settled.id
    assert db.query(UsageEvent).filter(UsageEvent.request_id == req_id).count() == 1

    # Convenience function settle_streamed_synthesis with same request_id also dedupes
    deduped = settle_streamed_synthesis(
        request_id=req_id,
        characters=180,
        user_id=user.id,
        min_characters=0,
        db=db,
    )
    assert deduped.id == settled.id
    assert db.query(UsageEvent).filter(UsageEvent.request_id == req_id).count() == 1


def test_weekly_aggregation_enforces_caps_via_over_limit_and_stays_auditable(db):
    """Given weekly aggregation, when voice usage is included, then voice units
    stay separately auditable and enforce caps via the same over_limit check."""
    # Free plan cap is 1,000,000
    free_user = make_user(db, "voice_capped_user", plan="free")

    now = datetime.utcnow()
    week = week_start(now)

    # 1. User uses 600,000 voice characters
    record_tts_usage(
        user_id=free_user.id,
        characters=600_000,
        voice_id="voice_v1",
        audio_seconds=1200.0,
        request_id="batch_1",
        apply_minimum=False,
        db=db,
    )

    # Voice usage is separately auditable
    auditable = voice_units_used(db, week, [free_user.id])
    assert free_user.id in auditable
    assert auditable[free_user.id]["characters"] == 600_000
    assert auditable[free_user.id]["audio_seconds"] == 1200.0
    assert auditable[free_user.id]["calls"] == 1

    # Tokens used includes voice characters in aggregate with 1.5x factor
    used = tokens_used(db, week, [free_user.id])
    assert used[free_user.id] == 900_000  # 600,000 * 1.5

    # User is under cap (900,000 < 1,000,000)
    assert over_limit(free_user.id) == ""

    # 2. User adds another 450,000 voice characters (Total = 1,050,000 > 1,000,000)
    record_tts_usage(
        user_id=free_user.id,
        characters=450_000,
        voice_id="voice_v1",
        audio_seconds=900.0,
        request_id="batch_2",
        apply_minimum=False,
        db=db,
    )

    # Voice units stay accurately auditable
    auditable_updated = voice_units_used(db, week, [free_user.id])
    assert auditable_updated[free_user.id]["characters"] == 1_050_000
    assert auditable_updated[free_user.id]["audio_seconds"] == 2100.0
    assert auditable_updated[free_user.id]["calls"] == 2

    # over_limit now blocks the user via the central quota enforcement
    blocked = over_limit(free_user.id)
    assert "this week's token limit" in blocked
    assert "Free plan" in blocked


def test_voice_usage_api_endpoints(db, client):
    """Test POST /api/voice/usage/tts and POST /api/voice/usage/stt HTTP endpoints."""
    token = "tok_voice_api"
    user = make_user(db, "api_voice_user", token=token)
    client.cookies.set(COOKIE, token)

    # Test TTS usage endpoint
    res_tts = client.post(
        "/api/voice/usage/tts",
        json={
            "characters": 320,
            "voice_id": "test_voice",
            "model": "eleven_multilingual_v2",
            "audio_seconds": 6.4,
            "request_id": "api_tts_1",
        },
    )
    assert res_tts.status_code == 200
    assert res_tts.json()["ok"] is True
    assert res_tts.json()["characters"] == 320

    # Test empty request_id is rejected (Finding 3)
    res_bad = client.post(
        "/api/voice/usage/tts",
        json={
            "characters": 100,
            "request_id": "",
        },
    )
    assert res_bad.status_code == 422

    # Test STT usage endpoint
    res_stt = client.post(
        "/api/voice/usage/stt",
        json={
            "audio_seconds": 45.0,
            "characters": 180,
            "model": "scribe_v1",
            "request_id": "api_stt_1",
        },
    )
    assert res_stt.status_code == 200
    assert res_stt.json()["ok"] is True
    assert res_stt.json()["characters"] == 180

    # Test /api/usage reports voice metrics
    res_usage = client.get("/api/usage")
    assert res_usage.status_code == 200
    data = res_usage.json()
    assert "voice_characters" in data["today"]
    assert data["today"]["voice_characters"] == 500  # 320 + 180
    assert data["today"]["voice_seconds"] == 0  # only live-call events carry billed seconds; these are legacy transcript events


# ── Voice calls are billed from ElevenLabs' record, settled by the server ──

import httpx
from fastapi import BackgroundTasks, HTTPException

from core.models import VoiceCall
from services import voice


def _call_setup(db, name):
    user = make_user(db, name)
    proj = Project(id=f"proj_{name}", name="Call Project")
    db.add(proj)
    db.commit()
    db.add(ProjectMember(project_id=proj.id, user_id=user.id, role="owner"))
    agent = Agent(id=f"agent_{name}", name="CallBot", system_prompt="test", project_id=proj.id)
    db.add(agent)
    db.commit()
    return user, proj, agent


def _call(db, user, proj, agent, call_id="c1", age_secs=0):
    call = VoiceCall(id=call_id, user_id=user.id, agent_id=agent.id, project_id=proj.id, started_at=datetime.utcnow() - timedelta(seconds=age_secs))
    db.add(call)
    db.commit()
    return call


def _eleven(monkeypatch, *replies):
    """ElevenLabs answers the list call with each reply in turn (the last one repeats). A reply is a list of conversations,
    an int HTTP status, an exception, or a raw JSON body."""
    monkeypatch.setenv("ELEVENLABS_API_KEY", "k")
    monkeypatch.setenv("ELEVENLABS_AGENT_ID", "agent_x")
    queue, seen = list(replies), []

    def get(url, params=None, headers=None, timeout=None):
        seen.append(params)
        reply = queue.pop(0) if len(queue) > 1 else queue[0]
        if isinstance(reply, Exception):
            raise reply
        status = reply if isinstance(reply, int) else 200
        body = {"conversations": reply} if isinstance(reply, list) else (reply if not isinstance(reply, int) else {})
        request = httpx.Request("GET", url)
        return httpx.Response(status, json=body, request=request)
    monkeypatch.setattr(voice.httpx, "get", get)
    return seen


def _conv(cid="conv_abcdef12", secs=90, status="done"):
    return {"conversation_id": cid, "call_duration_secs": secs, "status": status, "agent_id": "agent_x", "start_time_unix_secs": 0}


def _events(db, user):
    return db.query(UsageEvent).filter(UsageEvent.user_id == user.id).all()


def test_finished_call_is_billed_once_at_elevenlabs_length(db, monkeypatch):
    user, proj, agent = _call_setup(db, "settle_ok")
    call = _call(db, user, proj, agent)
    seen = _eleven(monkeypatch, [_conv(secs=90)])

    assert voice.settle(user.id, db) == 0
    assert voice.settle(user.id, db) == 0  # nothing left to do, and nothing is billed twice

    (event,) = _events(db, user)
    assert (event.kind, event.audio_seconds, event.characters, event.request_id) == ("call", 90.0, 0, "conv:conv_abcdef12")
    assert (event.agent_id, event.project_id) == (agent.id, None)  # the owner's own project is not an invited scope
    assert seen == [{"agent_id": "agent_x", "user_id": "c1", "page_size": 10}]  # found by the server's own call id
    assert db.get(VoiceCall, "c1").settled
    assert tokens_used(db, week_start(datetime.utcnow()), [user.id])[user.id] == 90 * 30


def test_call_still_processing_is_not_billed_until_done(db, monkeypatch):
    """A partial duration must never take the conversation's key: it is billed only once ElevenLabs says done."""
    user, proj, agent = _call_setup(db, "settle_proc")
    _call(db, user, proj, agent)
    _eleven(monkeypatch, [_conv(secs=4, status="processing")], [_conv(secs=1800, status="done")])

    assert voice.settle(user.id, db) == 1
    assert _events(db, user) == [] and not db.get(VoiceCall, "c1").settled
    assert voice.settle(user.id, db) == 0
    assert [e.audio_seconds for e in _events(db, user)] == [1800.0]


@pytest.mark.parametrize("reply", [404, 429, 500, httpx.ConnectError("down"), ["not", "a", "dict"], {"conversations": "x"}])
def test_elevenlabs_errors_defer_and_never_raise(db, monkeypatch, reply):
    user, proj, agent = _call_setup(db, "settle_err")
    _call(db, user, proj, agent)
    _eleven(monkeypatch, reply, [_conv()])

    assert voice.settle(user.id, db) == 1  # deferred, not lost and not billed
    assert _events(db, user) == []
    assert voice.settle(user.id, db) == 0  # the next attempt bills it
    assert len(_events(db, user)) == 1


def test_unconfigured_elevenlabs_defers(db, monkeypatch):
    monkeypatch.delenv("ELEVENLABS_API_KEY", raising=False)
    user, proj, agent = _call_setup(db, "settle_cfg")
    _call(db, user, proj, agent)
    assert voice.settle(user.id, db) == 1 and _events(db, user) == []


def test_two_calls_of_the_same_length_are_both_billed(db, monkeypatch):
    user, proj, agent = _call_setup(db, "settle_two")
    _call(db, user, proj, agent, "c1")
    _call(db, user, proj, agent, "c2")
    _eleven(monkeypatch, [_conv("conv_aaaaaaaa", 60)], [_conv("conv_bbbbbbbb", 60)])

    assert voice.settle(user.id, db) == 0
    assert sorted(e.request_id for e in _events(db, user)) == ["conv:conv_aaaaaaaa", "conv:conv_bbbbbbbb"]


def test_call_that_never_connected_bills_nothing_and_is_dropped(db, monkeypatch):
    user, proj, agent = _call_setup(db, "settle_none")
    _call(db, user, proj, agent, "fresh", age_secs=10)
    _call(db, user, proj, agent, "stale", age_secs=voice._NO_CALL_SECS + 60)
    _eleven(monkeypatch, [])

    assert voice.settle(user.id, db) == 1  # the fresh one may still connect
    assert db.get(VoiceCall, "stale").settled and not db.get(VoiceCall, "fresh").settled
    assert _events(db, user) == []


def test_call_older_than_a_day_is_abandoned_without_asking(db, monkeypatch):
    user, proj, agent = _call_setup(db, "settle_old")
    _call(db, user, proj, agent, age_secs=voice._GIVE_UP_SECS + 60)
    seen = _eleven(monkeypatch, [_conv()])
    assert voice.settle(user.id, db) == 0 and seen == [] and db.get(VoiceCall, "c1").settled and _events(db, user) == []


def test_oversized_or_odd_conversation_ids_are_not_billed(db, monkeypatch):
    user, proj, agent = _call_setup(db, "settle_ids")
    _call(db, user, proj, agent)
    _eleven(monkeypatch, [_conv("x" * 60), _conv("bad id!"), {"conversation_id": None, "status": "done", "call_duration_secs": 5}, "junk", _conv("conv_okok1234", 7)])
    voice.settle(user.id, db)
    assert [e.request_id for e in _events(db, user)] == ["conv:conv_okok1234"]


def test_already_billed_conversation_is_absorbed_but_other_errors_are_not(db, monkeypatch):
    from core.usage import record_call_usage
    user, proj, agent = _call_setup(db, "settle_dup")
    kw = dict(seconds=30, agent_id=agent.id, conversation_id="conv_dupdup12")
    assert record_call_usage(db, user.id, **kw) is not None
    db.commit()
    assert record_call_usage(db, user.id, **kw) is None  # a duplicate (or the loser of a concurrent run) is a no-op
    assert len(_events(db, user)) == 1
    monkeypatch.setattr(db, "add", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("db is down")))
    with pytest.raises(RuntimeError):
        record_call_usage(db, user.id, **{**kw, "conversation_id": "conv_other123"})


def test_settle_soon_retries_then_stops_and_survives_errors(monkeypatch):
    answers = iter([2, 1, 0])
    monkeypatch.setattr(voice, "settle", lambda uid: next(answers))
    sleeps = []
    monkeypatch.setattr(voice.time, "sleep", sleeps.append)
    voice.settle_soon("u1", tries=6, wait=5)
    assert sleeps == [5, 5] and "u1" not in voice._settling

    def boom(uid):
        raise RuntimeError("x")
    monkeypatch.setattr(voice, "settle", boom)
    voice.settle_soon("u1")  # logged, not raised
    assert "u1" not in voice._settling

    voice._settling.add("u2")
    monkeypatch.setattr(voice, "settle", lambda uid: pytest.fail("a second loop for the same person must not start"))
    voice.settle_soon("u2")
    voice._settling.discard("u2")


def test_start_voice_records_the_call(db, monkeypatch):
    from routes.agents import start_voice
    user, proj, agent = _call_setup(db, "start_rec")
    monkeypatch.setattr(voice, "signed_url", lambda: "wss://x")
    monkeypatch.setenv("CONNECTIONS_KEY", "k")
    background = BackgroundTasks()
    out = start_voice(agent.id, background, user=user, project=proj, db=db)
    call = db.get(VoiceCall, out["call_id"])
    assert (call.user_id, call.agent_id, call.project_id, call.settled) == (user.id, agent.id, proj.id, False)
    assert [t.func for t in background.tasks] == [voice.settle_soon]


def test_save_voice_call_needs_the_users_own_call_and_never_bills(db, monkeypatch):
    from routes.agents import CallLine, CallTranscript, save_voice_call
    user, proj, agent = _call_setup(db, "save_own")
    other = make_user(db, "save_other")
    _call(db, user, proj, agent, "mine")
    lines = [CallLine(who="bot", text="hi")]
    background = BackgroundTasks()

    with pytest.raises(HTTPException) as err:  # a call id the server never issued
        save_voice_call(agent.id, CallTranscript(secs=1, call_id="nope", lines=lines), background, user=user, project=proj, db=db)
    assert err.value.status_code == 404
    for bad in ({}, {"call_id": ""}):  # no call id at all is refused by the schema
        with pytest.raises(Exception):
            CallTranscript(secs=1, lines=lines, **bad)
    with pytest.raises(HTTPException) as err:  # someone else's call id
        save_voice_call(agent.id, CallTranscript(secs=1, call_id="mine", lines=lines), background, user=other, project=proj, db=db)
    assert err.value.status_code == 404

    save_voice_call(agent.id, CallTranscript(secs=3600, call_id="mine", lines=lines), BackgroundTasks(), user=user, project=proj, db=db)
    assert _events(db, user) == []  # a claimed hour bills nothing: only settle() bills
