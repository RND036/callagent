import json
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from dashboard.server import create_app
from data.call_manager import CallManager
from data.json_store import JsonBookingStore
from data.settings_store import SalonSettings, SettingsStore
from gemini_session.persona import build_initial_greeting, build_system_instruction


@pytest.fixture
def temp_settings_path(tmp_path: Path) -> Path:
    return tmp_path / "test_settings.json"


@pytest.fixture
def settings_mgr(temp_settings_path: Path) -> SettingsStore:
    return SettingsStore(settings_path=temp_settings_path)


@pytest.fixture
def mock_store(tmp_path: Path) -> JsonBookingStore:
    services_path = tmp_path / "services.json"
    services_path.write_text(json.dumps([{"name": "Manicure", "price_usd": 20.0, "duration_minutes": 30}]))
    bookings_path = tmp_path / "bookings.json"
    bookings_path.write_text("[]")
    messages_path = tmp_path / "messages.json"
    messages_path.write_text("[]")
    return JsonBookingStore(
        services_path=services_path,
        bookings_path=bookings_path,
        messages_path=messages_path,
        business_hours_start="09:00",
        business_hours_end="19:00",
        slot_increment_minutes=30,
    )


@pytest.mark.asyncio
async def test_settings_store_defaults_and_update(settings_mgr: SettingsStore, temp_settings_path: Path):
    s = await settings_mgr.get_settings()
    assert s.salon_name == "CLASSY NAIL SPA"
    assert s.business_hours_start == "09:00"

    updated = await settings_mgr.update_settings({
        "salon_name": "Luxe Glow Salon",
        "salon_phone": "+94 77 999 8888",
        "business_hours_start": "10:00",
    })
    assert updated.salon_name == "Luxe Glow Salon"
    assert updated.salon_phone == "+94 77 999 8888"
    assert updated.business_hours_start == "10:00"

    # Verify persisted to disk
    assert temp_settings_path.exists()
    disk_data = json.loads(temp_settings_path.read_text())
    assert disk_data["salon_name"] == "Luxe Glow Salon"

    # Reload from disk
    reloaded_mgr = SettingsStore(settings_path=temp_settings_path)
    s2 = await reloaded_mgr.get_settings()
    assert s2.salon_name == "Luxe Glow Salon"


def test_dynamic_system_instruction_and_greeting():
    custom = SalonSettings(
        salon_name="Luxe Glow",
        salon_address="45 Flower Road, Colombo 07",
        salon_address_sinhala="අංක හතළිහ පහ, මල් පාර, කොළඹ හත",
        salon_phone="+94 11 234 5678",
        transfer_phone="+94112345678",
        operating_hours_display="Daily 10 AM to 8 PM",
        greeting_sinhala="ආයුබෝවන්! {salon_name} වෙත සාදරයෙන් පිළිගනිමු.",
    )

    greeting = build_initial_greeting(custom)
    assert "ආයුබෝවන්! Luxe Glow වෙත සාදරයෙන් පිළිගනිමු." in greeting

    instruction = build_system_instruction(custom)
    assert "LUXE GLOW" in instruction
    assert "45 Flower Road, Colombo 07" in instruction
    assert "say it as: අංක හතළිහ පහ, මල් පාර, කොළඹ හත" in instruction
    assert "+94 11 234 5678" in instruction
    assert "Daily 10 AM to 8 PM" in instruction
    assert 'target_number="+94112345678"' in instruction

    # Test dynamic services injection into persona prompt
    from data.store import Service
    custom_services = [Service(name="Diamond Gel Nail Art", price_usd=60.0, duration_minutes=90)]
    instruction_with_services = build_system_instruction(custom, services=custom_services)
    assert "Diamond Gel Nail Art" in instruction_with_services
    assert "Diamond Gel Nail Art (LKR" not in instruction_with_services  # prices only via tools, when asked


def test_greeting_per_ivr_language_uses_its_own_setting():
    custom = SalonSettings(
        salon_name="Luxe Glow",
        greeting_tamil="வணக்கம் {salon_name}!",
        greeting_english="Hi from {salon_name}!",
    )

    assert build_initial_greeting(custom, "ta") == "வணக்கம் Luxe Glow!"
    assert build_initial_greeting(custom, "en") == "Hi from Luxe Glow!"
    assert '"Hi from Luxe Glow!"' in build_system_instruction(custom, language="en")


@pytest.mark.asyncio
async def test_settings_api_endpoints(mock_store: JsonBookingStore, settings_mgr: SettingsStore, tmp_path: Path):
    call_mgr = CallManager(calls_path=tmp_path / "calls.json")
    app = create_app(mock_store, call_mgr, settings_mgr)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        res = await client.post("/api/login", json={"username": "admin", "password": "admin"})
        assert res.status_code == 200

        # GET
        res = await client.get("/api/settings")
        assert res.status_code == 200
        data = res.json()
        assert "salon_name" in data
        assert "greeting_sinhala" in data

        # POST Update
        res_update = await client.post("/api/settings", json={
            "salon_name": "Royal Nails",
            "salon_phone": "+94 71 111 2222",
            "business_hours_start": "08:00",
            "business_hours_end": "20:00",
        })
        assert res_update.status_code == 200
        assert res_update.json()["status"] == "success"
        assert res_update.json()["settings"]["salon_name"] == "Royal Nails"

        # Verify BookingStore business hours updated
        assert mock_store._business_hours_start == "08:00"
        assert mock_store._business_hours_end == "20:00"

        # Verify GET reflects update
        res_get = await client.get("/api/settings")
        assert res_get.json()["salon_name"] == "Royal Nails"


# ---------- AI voice / temperature / pause ----------

def test_ai_settings_validation():
    from data.settings_store import validate_ai_settings

    assert validate_ai_settings({"ai_voice": "Kore", "ai_temperature": "0.70", "ai_vad_silence_ms": "800"}) == {
        "ai_voice": "Kore", "ai_temperature": "0.7", "ai_vad_silence_ms": 800}
    assert validate_ai_settings({"ai_temperature": " "})["ai_temperature"] == ""  # blank = model default
    for bad in ({"ai_voice": "Nope"}, {"ai_temperature": "3"}, {"ai_temperature": "hot"}, {"ai_vad_silence_ms": 50}):
        with pytest.raises(ValueError):
            validate_ai_settings(bad)


def test_live_config_uses_dashboard_voice_temperature_and_pause():
    from data.settings_store import SalonSettings
    from gemini_session.live_session import live_config

    cfg = live_config(None, "en", None, SalonSettings(ai_voice="Kore", ai_temperature="0.6", ai_vad_silence_ms=1500))
    assert cfg.speech_config.voice_config.prebuilt_voice_config.voice_name == "Kore"
    assert cfg.temperature == 0.6
    assert cfg.realtime_input_config.automatic_activity_detection.silence_duration_ms == 1500
    blank = live_config(None, "en", None, SalonSettings(ai_temperature=""))
    assert blank.temperature is None  # model default unless GEMINI_TEMPERATURE is set in .env


@pytest.mark.asyncio
async def test_settings_endpoint_saves_ai_fields_and_rejects_bad_ones(tmp_path):
    from httpx import ASGITransport, AsyncClient

    from dashboard.server import create_app
    from data.call_manager import CallManager
    from data.json_store import JsonBookingStore
    from data.settings_store import SettingsStore

    for name in ("services", "bookings", "messages"):
        (tmp_path / f"{name}.json").write_text("[]")
    sstore = SettingsStore(settings_path=tmp_path / "s.json")
    store = JsonBookingStore(services_path=tmp_path / "services.json", bookings_path=tmp_path / "bookings.json",
                             messages_path=tmp_path / "messages.json", business_hours_start="09:00",
                             business_hours_end="19:00", slot_increment_minutes=30)
    app = create_app(store, CallManager(calls_path=tmp_path / "calls.json"), sstore)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        assert (await c.post("/api/login", json={"username": "admin", "password": "admin"})).status_code == 200
        r = await c.post("/api/settings", json={"ai_voice": "Puck", "ai_temperature": "0.8"})
        assert r.status_code == 200, r.text
        saved = await sstore.get_settings()
        assert saved.ai_voice == "Puck" and saved.ai_temperature == "0.8"
        assert (await c.post("/api/settings", json={"ai_temperature": "9"})).status_code == 422
        assert "Puck" in (await c.get("/api/settings")).json()["ai_voices"]
