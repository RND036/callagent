from contextlib import asynccontextmanager
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from dashboard.server import create_app
from data.call_manager import CallManager
from data.faq_store import MAX_PROMPT_CHARS, SEED_FAQS, FaqStore, faq_prompt_lines
from data.json_store import JsonBookingStore
from data.settings_store import SettingsStore
from tools.context import CallContext


@pytest.mark.asyncio
async def test_seed_crud_and_persistence(tmp_path: Path):
    path = tmp_path / "faqs.json"
    store = FaqStore(path)
    seeded = await store.list_faqs()
    assert len(seeded) == len(SEED_FAQS) and path.exists()

    added = await store.upsert({"question": "Parking?", "answer": "Yes, free."})
    assert added["enabled"] is True and added["faq_id"]
    updated = await store.upsert({"answer": "Yes, behind the building."}, added["faq_id"])
    assert updated["question"] == "Parking?" and updated["answer"].startswith("Yes, behind")
    with pytest.raises(ValueError):
        await store.upsert({"question": "  "})
    with pytest.raises(KeyError):
        await store.upsert({"question": "x"}, "nope")

    reloaded = FaqStore(path)  # a restart keeps the edits and does not re-seed
    assert any(f["answer"] == "Yes, behind the building." for f in await reloaded.list_faqs())
    assert await reloaded.delete(added["faq_id"]) and not await reloaded.delete(added["faq_id"])


def test_prompt_lines_skip_disabled_and_empty_and_respect_the_cap(caplog):
    faqs = [
        {"question": "A?", "answer": "a", "enabled": True},
        {"question": "B?", "answer": "b", "enabled": False},
        {"question": "C?", "answer": "  ", "enabled": True},
    ]
    assert faq_prompt_lines(faqs) == ["Q: A?\nA: a"]
    big = [{"question": f"Q{i}?", "answer": "x" * 500, "enabled": True} for i in range(40)]
    blocks = faq_prompt_lines(big)
    assert 0 < len(blocks) < 40 and sum(len(b) for b in blocks) <= MAX_PROMPT_CHARS
    assert "longer than" in caplog.text


@pytest.mark.asyncio
async def test_build_instruction_includes_the_faq(tmp_path: Path):
    from gemini_session.live_session import build_instruction

    faq = FaqStore(tmp_path / "faqs.json")
    await faq.upsert({"question": "Is there parking?", "answer": "Free parking behind the salon."})
    ctx = CallContext(store=None, mcp=None, call_sid="c", other_leg_call_id=None, account_id=None,
                      caller_number=None, faq_store=faq, language="en")
    instruction, _ = await build_instruction(ctx)
    assert "Q: Is there parking?\nA: Free parking behind the salon." in instruction
    assert "Hokandara: (address not set yet)" in instruction  # seeded entries are there too


@asynccontextmanager
async def _client(tmp_path: Path):
    for name in ("services", "bookings", "messages"):
        (tmp_path / f"{name}.json").write_text("[]")
    store = JsonBookingStore(tmp_path / "services.json", tmp_path / "bookings.json", tmp_path / "messages.json",
                             "09:00", "19:00", 30)
    app = create_app(store, CallManager(calls_path=tmp_path / "calls.json"),
                     SettingsStore(settings_path=tmp_path / "s.json"), faq_store=FaqStore(tmp_path / "faqs.json"))
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as client:
        assert (await client.post("/api/login", json={"username": "admin", "password": "admin"})).status_code == 200
        client.anonymous = lambda: AsyncClient(transport=transport, base_url="http://t")
        yield client, store


@pytest.mark.asyncio
async def test_faq_api_needs_login_and_supports_crud(tmp_path: Path):
    async with _client(tmp_path) as (client, _):
        faq = (await client.post("/api/faqs", json={"question": "Parking?", "answer": "Yes"})).json()
        assert (await client.post("/api/faqs", json={"question": ""})).status_code == 400
        assert (await client.put(f"/api/faqs/{faq['faq_id']}", json={"enabled": False})).json()["enabled"] is False
        assert (await client.put("/api/faqs/missing", json={"answer": "x"})).status_code == 404
        assert any(f["faq_id"] == faq["faq_id"] for f in (await client.get("/api/faqs")).json())
        assert (await client.delete(f"/api/faqs/{faq['faq_id']}")).status_code == 200
        assert (await client.delete(f"/api/faqs/{faq['faq_id']}")).status_code == 404
        async with client.anonymous() as anon:
            assert (await anon.get("/api/faqs")).status_code == 401


@pytest.mark.asyncio
async def test_booking_actions_message_done_services_and_settings(tmp_path: Path):
    async with _client(tmp_path) as (client, store):
        await store.upsert_service(__import__("data.store", fromlist=["Service"]).Service("Manicure", 2500.0, 30))
        b = await store.create_booking("Manicure", "2030-01-01", "10:00", "Nimali", "077", branch="Nugegoda")
        assert b.status == "requested"
        assert (await client.post(f"/api/bookings/{b.booking_id}/confirm")).status_code == 200
        assert (await store.get_booking(b.booking_id)).status == "confirmed"

        await store.request_change(b.booking_id, "2030-01-02", "11:00")
        stats = (await client.get("/api/stats")).json()
        assert stats["requested_bookings_count"] == 1  # a pending change needs attention too
        assert (await client.post(f"/api/bookings/{b.booking_id}/change/approve")).status_code == 200
        moved = await store.get_booking(b.booking_id)
        assert (moved.date, moved.time) == ("2030-01-02", "11:00")
        await store.request_change(b.booking_id, "2030-01-03", "12:00")
        assert (await client.post(f"/api/bookings/{b.booking_id}/change/reject")).status_code == 200
        assert (await store.get_booking(b.booking_id)).requested_date == ""
        assert (await client.post(f"/api/bookings/{b.booking_id}/decline")).status_code == 200
        assert (await client.post("/api/bookings/nope/confirm")).status_code == 404

        msg = await store.take_message("A", "1", "complaint", urgent=True, category="complaint")
        assert (await client.get("/api/stats")).json()["urgent_messages_count"] == 1
        assert (await client.post(f"/api/messages/{msg['message_id']}/done")).status_code == 200
        assert (await client.get("/api/stats")).json()["urgent_messages_count"] == 0
        assert (await client.post("/api/messages/nope/done")).status_code == 404

        svc = await client.post("/api/services", json={"name": "Gel extensions", "price_usd": 9000,
                                                       "duration_minutes": 90, "price_varies": True})
        assert svc.json()["price_varies"] is True
        assert any(s["price_varies"] for s in (await client.get("/api/services")).json())

        ok = await client.post("/api/settings", json={"branches": "Nugegoda,  Hokandara", "whatsapp_number": "+94 77 1"})
        assert ok.status_code == 200 and ok.json()["settings"]["branches"] == "Nugegoda, Hokandara"
        for bad in ({"branches": " , "}, {"branches": "A, a"}, {"whatsapp_number": "call me"}):
            assert (await client.post("/api/settings", json=bad)).status_code == 422
