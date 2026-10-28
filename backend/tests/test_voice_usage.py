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


def _call_setup(db, name):
    from services import voice  # noqa: F401
    user = make_user(db, name)
    proj = Project(id=f"proj_{name}", name="Call Project")
    db.add(proj)
    db.commit()
    db.add(ProjectMember(project_id=proj.id, user_id=user.id, role="owner"))
    agent = Agent(id=f"agent_{name}", name="CallBot", system_prompt="test", project_id=proj.id)
    db.add(agent)
    db.commit()
    return user, proj, agent


def _save(db, user, proj, agent, **fields):
    from routes.agents import CallLine, CallTranscript, save_voice_call
    body = CallTranscript(secs=fields.pop("secs", 5), lines=[CallLine(who="bot", text="hi")], **fields)
    save_voice_call(agent.id, body, user=user, project=proj, db=db)


def test_call_is_billed_by_elevenlabs_duration_once(db, monkeypatch):
    """A confirmed call is one event at ElevenLabs' length, whatever the browser says, and saving it again adds nothing."""
    from services import voice
    user, proj, agent = _call_setup(db, "call_conf")
    monkeypatch.setattr(voice, "call_seconds", lambda cid, uid: 90.0 if (cid, uid) == ("conv_abcdef12", user.id) else None)

    _save(db, user, proj, agent, secs=3, conversation_id="conv_abcdef12")
    _save(db, user, proj, agent, secs=3, conversation_id="conv_abcdef12")

    events = db.query(UsageEvent).filter(UsageEvent.user_id == user.id).all()
    assert [(e.kind, e.audio_seconds, e.characters, e.request_id) for e in events] == [("call", 90.0, 0, "conv:conv_abcdef12")]
    assert tokens_used(db, week_start(datetime.utcnow()), [user.id])[user.id] == 90 * 30


def test_unconfirmed_call_uses_server_clock_not_the_browser(db, monkeypatch):
    """With no ElevenLabs answer, the length is the time since the server minted the call's token; a claimed 1 second does not make it free."""
    import time
    from services import voice
    user, proj, agent = _call_setup(db, "call_clock")
    monkeypatch.setattr(voice, "call_seconds", lambda cid, uid: None)
    token = voice.call_token(user.id, agent.id)
    real = time.time()
    monkeypatch.setattr(time, "time", lambda: real + 120)

    _save(db, user, proj, agent, secs=1, voice_token=token, conversation_id="whatever")

    (event,) = db.query(UsageEvent).filter(UsageEvent.user_id == user.id).all()
    assert event.kind == "call" and 119 <= event.audio_seconds <= 121
    assert event.request_id.startswith("call:")  # an unconfirmed claim never takes a conversation's key


def test_call_seconds_rejects_another_users_conversation(monkeypatch):
    from services import voice
    monkeypatch.setenv("ELEVENLABS_API_KEY", "k")
    monkeypatch.setenv("ELEVENLABS_AGENT_ID", "agent_x")
    reply = lambda user_id, agent_id="agent_x": type("R", (), {"raise_for_status": lambda s: None, "json": lambda s: {
        "agent_id": agent_id, "user_id": user_id, "status": "done", "metadata": {"call_duration_secs": 42}}})()
    monkeypatch.setattr(voice.httpx, "get", lambda *a, **k: reply("me"))
    assert voice.call_seconds("conv_abcdef12", "me") == 42.0
    assert voice.call_seconds("conv_abcdef12", "someone_else") is None
    monkeypatch.setattr(voice.httpx, "get", lambda *a, **k: reply("me", agent_id="other_agent"))
    assert voice.call_seconds("conv_abcdef12", "me") is None
    assert voice.call_seconds("../x", "me") is None
