import asyncio
import json
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from dashboard.server import create_app
from data.call_manager import CallManager
from data.json_store import JsonBookingStore


@pytest.fixture
def test_store(tmp_path: Path) -> JsonBookingStore:
    services_path = tmp_path / "services.json"
    services_path.write_text(
        json.dumps([
            {"name": "Manicure", "price_usd": 25.0, "duration_minutes": 30},
            {"name": "Pedicure", "price_usd": 35.0, "duration_minutes": 45},
        ])
    )
    bookings_path = tmp_path / "bookings.json"
    bookings_path.write_text(
        json.dumps([
            {
                "booking_id": "b-test1",
                "service_name": "Manicure",
                "date": "2026-10-10",
                "time": "10:00",
                "customer_name": "Alice Smith",
                "customer_phone": "555-0101",
                "status": "confirmed",
            }
        ])
    )
    messages_path = tmp_path / "messages.json"
    messages_path.write_text(
        json.dumps([
            {
                "caller_name": "Bob",
                "caller_phone": "555-0202",
                "message": "Please call me back.",
                "urgent": True,
                "taken_at": "2026-10-10T09:30:00",
            }
        ])
    )
    return JsonBookingStore(
        services_path=services_path,
        bookings_path=bookings_path,
        messages_path=messages_path,
        business_hours_start="09:00",
        business_hours_end="18:00",
        slot_increment_minutes=30,
    )


@pytest.fixture
def call_mgr(tmp_path: Path) -> CallManager:
    return CallManager(calls_path=tmp_path / "calls.json")


@pytest.mark.asyncio
async def test_call_manager_lifecycle(call_mgr: CallManager):
    q = call_mgr.subscribe()

    rec = await call_mgr.call_started("call-123", "+15551234")
    assert rec.call_id == "call-123"
    assert rec.status == "in-call"

    event1 = await q.get()
    assert event1["type"] == "call_started"
    assert event1["data"]["call_id"] == "call-123"

    active = await call_mgr.get_active_calls()
    assert len(active) == 1

    await call_mgr.record_action("call-123", "User asked about prices")
    event2 = await q.get()
    assert event2["type"] == "call_action"

    ended = await call_mgr.call_ended("call-123", status="completed")
    assert ended is not None
    assert ended.status == "completed"

    event3 = await q.get()
    assert event3["type"] == "call_ended"

    active_after = await call_mgr.get_active_calls()
    assert len(active_after) == 0

    history = await call_mgr.get_call_history()
    assert len(history) == 1
    assert history[0]["call_id"] == "call-123"


@pytest.mark.asyncio
async def test_dashboard_api_endpoints(test_store: JsonBookingStore, call_mgr: CallManager):
    app = create_app(test_store, call_mgr)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # Unauthenticated API calls are rejected
        res = await client.get("/api/stats")
        assert res.status_code == 401

        # Wrong credentials are rejected
        res = await client.post("/api/login", json={"username": "admin", "password": "wrong"})
        assert res.status_code == 401

        # Correct credentials establish a session cookie
        res = await client.post("/api/login", json={"username": "admin", "password": "admin"})
        assert res.status_code == 200

        # Test stats
        res = await client.get("/api/stats")
        assert res.status_code == 200
        stats = res.json()
        assert stats["confirmed_bookings_count"] == 1
        assert stats["urgent_messages_count"] == 1
        assert stats["active_calls_count"] == 0

        # Test services
        res = await client.get("/api/services")
        assert res.status_code == 200
        services = res.json()
        assert len(services) == 2

        # Test bookings
        res = await client.get("/api/bookings")
        assert res.status_code == 200
        bookings = res.json()
        assert len(bookings) == 1
        assert bookings[0]["booking_id"] == "b-test1"

        # Test cancel booking
        res = await client.post("/api/bookings/b-test1/cancel")
        assert res.status_code == 200
        assert res.json()["status"] == "success"

        # Verify cancelled
        res = await client.get("/api/bookings")
        assert res.json()[0]["status"] == "cancelled"

        # Test messages
        res = await client.get("/api/messages")
        assert res.status_code == 200
        messages = res.json()
        assert len(messages) == 1
        assert messages[0]["caller_name"] == "Bob"

        # Test create service
        res = await client.post("/api/services", json={"name": "Nail Art Deluxe", "price_usd": 45.0, "duration_minutes": 60})
        assert res.status_code == 200
        assert res.json()["name"] == "Nail Art Deluxe"
        assert res.json()["price_usd"] == 45.0

        # Test update service
        res = await client.put("/api/services/Nail Art Deluxe", json={"name": "Nail Art Deluxe", "price_usd": 50.0, "duration_minutes": 75})
        assert res.status_code == 200
        assert res.json()["price_usd"] == 50.0

        # Test delete service
        res = await client.delete("/api/services/Nail Art Deluxe")
        assert res.status_code == 200
        assert res.json()["status"] == "deleted"

        # Verify service deleted
        res = await client.get("/api/services")
        names = [s["name"] for s in res.json()]
        assert "Nail Art Deluxe" not in names

        # Test index page
        res = await client.get("/")
        assert res.status_code == 200
        assert "Classy Nail Spa" in res.text


@pytest.mark.asyncio
async def test_call_history_keeps_newest_calls_and_prunes_old_recordings(tmp_path: Path, monkeypatch):
    import data.call_manager as cm

    monkeypatch.setattr(cm, "_MAX_COMPLETED_CALLS", 3)
    recordings = tmp_path / "rec"
    recordings.mkdir()
    calls_path = tmp_path / "calls.json"
    manager = CallManager(calls_path=calls_path, recordings_dir=recordings)

    for i in range(5):
        (recordings / f"c{i}.wav").write_bytes(b"x")
        await manager.call_started(f"c{i}", "+1")
        await manager.call_ended(f"c{i}", recording_file=f"c{i}.wav")

    history = await manager.get_call_history()
    assert [c["call_id"] for c in history] == ["c4", "c3", "c2"]
    assert sorted(p.name for p in recordings.glob("*.wav")) == ["c2.wav", "c3.wav", "c4.wav"]
    # The newest call is what was persisted, so it survives a restart.
    reloaded = CallManager(calls_path=calls_path)
    assert [c["call_id"] for c in await reloaded.get_call_history()] == ["c4", "c3", "c2"]


@pytest.mark.asyncio
async def test_call_recording_endpoint(test_store: JsonBookingStore, tmp_path: Path):
    manager = CallManager(calls_path=tmp_path / "calls.json", recordings_dir=tmp_path)
    (tmp_path / "abc.wav").write_bytes(b"RIFFfakewav")
    await manager.call_started("abc", "+94771234567")
    await manager.call_ended("abc", recording_file="abc.wav")
    await manager.call_started("silent", "+94770000000")
    await manager.call_ended("silent")

    app = create_app(test_store, manager)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        assert (await client.get("/api/calls/abc/recording")).status_code == 401

        await client.post("/api/login", json={"username": "admin", "password": "admin"})
        res = await client.get("/api/calls/abc/recording")
        assert res.status_code == 200
        assert res.headers["content-type"] == "audio/wav"
        assert res.content == b"RIFFfakewav"

        assert (await client.get("/api/calls/silent/recording")).status_code == 404
        assert (await client.get("/api/calls/nope/recording")).status_code == 404

        detail = await client.get("/api/calls/abc")
        assert detail.status_code == 200 and detail.json()["recording_file"] == "abc.wav"
        assert (await client.get("/api/calls/nope")).status_code == 404


@pytest.mark.asyncio
async def test_ivr_endpoints(test_store: JsonBookingStore, tmp_path: Path):
    from data.ivr_store import IvrStore
    from tests.test_ivr_store import make_wav

    ivr = IvrStore(tmp_path / "ivr.json", tmp_path / "prompts")
    app = create_app(test_store, CallManager(calls_path=tmp_path / "calls.json"), ivr_store=ivr)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        assert (await client.get("/api/ivr")).status_code == 401
        await client.post("/api/login", json={"username": "admin", "password": "admin"})

        state = (await client.get("/api/ivr")).json()
        assert state["enabled"] is False and state["active"] is False and state["languages"]["ta"] == "Tamil"

        bad = await client.post("/api/ivr", json={"options": [{"digit": "1", "language": "si"}, {"digit": "1", "language": "en"}]})
        assert bad.status_code == 422

        assert (await client.put("/api/ivr/prompts/nope", content=make_wav())).status_code == 404
        assert (await client.put("/api/ivr/prompts/menu", content=b"not audio")).status_code == 400
        up = await client.put("/api/ivr/prompts/menu", params={"filename": "m.wav"}, content=make_wav())
        assert up.status_code == 200 and up.json()["ivr"]["prompts"]["menu"]["original_name"] == "m.wav"

        ok = await client.post("/api/ivr", json={"enabled": True})
        assert ok.status_code == 200 and ok.json()["ivr"]["active"] is True

        audio = await client.get("/api/ivr/prompts/menu")
        assert audio.status_code == 200 and audio.headers["content-type"] == "audio/wav"
        assert (await client.get("/api/ivr/prompts/invalid")).status_code == 404

        assert (await client.delete("/api/ivr/prompts/menu")).json()["ivr"]["active"] is False
        assert (await client.get("/api/ivr/prompts/menu")).status_code == 404
