import shutil
from pathlib import Path

import pytest

import config
from data.json_store import JsonBookingStore
from tools import registry
from tools.context import CallContext


@pytest.fixture
def ctx(tmp_path: Path) -> CallContext:
    services_path = tmp_path / "seed_services.json"
    shutil.copy(config.SEED_SERVICES_PATH, services_path)
    store = JsonBookingStore(
        services_path=services_path,
        bookings_path=tmp_path / "bookings.json",
        messages_path=tmp_path / "messages.json",
        business_hours_start=config.BUSINESS_HOURS_START,
        business_hours_end=config.BUSINESS_HOURS_END,
        slot_increment_minutes=config.SLOT_INCREMENT_MINUTES,
    )
    return CallContext(
        store=store,
        mcp=None,
        call_sid="call-1",
        other_leg_call_id=None,
        account_id=None,
        caller_number="555-1234",
    )


@pytest.mark.asyncio
async def test_list_services(ctx: CallContext) -> None:
    result = await registry.dispatch("list_services", {}, ctx)
    names = {s["name"] for s in result["services"]}
    assert "Manicure" in names
    assert "Waterless pedicure" in names
    assert len(result["services"]) == 9


@pytest.mark.asyncio
async def test_quote_price_known_service(ctx: CallContext) -> None:
    result = await registry.dispatch("quote_price", {"service_name": "Gel manicure"}, ctx)
    assert result["name"] == "Gel manicure"
    assert result["price_usd"] > 0
    assert result["duration_minutes"] > 0


@pytest.mark.asyncio
async def test_quote_price_unknown_service(ctx: CallContext) -> None:
    result = await registry.dispatch("quote_price", {"service_name": "Hair cut"}, ctx)
    assert "error" in result


@pytest.mark.asyncio
async def test_check_availability_returns_slots(ctx: CallContext) -> None:
    result = await registry.dispatch(
        "check_availability", {"service_name": "Manicure", "date": "2026-10-01"}, ctx
    )
    assert result["available_times"]
    assert "09:00" in result["available_times"]


@pytest.mark.asyncio
async def test_create_booking_then_slot_unavailable(ctx: CallContext) -> None:
    booking_args = {
        "service_name": "Manicure",
        "date": "2026-10-01",
        "time": "09:00",
        "customer_name": "Jane Doe",
        "customer_phone": "555-1234",
    }
    result = await registry.dispatch("create_booking", booking_args, ctx)
    assert result["status"] == "confirmed"
    booking_id = result["booking_id"]

    availability = await registry.dispatch(
        "check_availability", {"service_name": "Manicure", "date": "2026-10-01"}, ctx
    )
    assert "09:00" not in availability["available_times"]

    duplicate = await registry.dispatch("create_booking", booking_args, ctx)
    assert "error" in duplicate

    modified = await registry.dispatch(
        "modify_booking", {"booking_id": booking_id, "new_time": "10:00"}, ctx
    )
    assert modified["time"] == "10:00"

    cancelled = await registry.dispatch("cancel_booking", {"booking_id": booking_id}, ctx)
    assert cancelled["status"] == "cancelled"


@pytest.mark.asyncio
async def test_modify_unknown_booking(ctx: CallContext) -> None:
    result = await registry.dispatch(
        "modify_booking", {"booking_id": "nope", "new_time": "10:00"}, ctx
    )
    assert "error" in result


@pytest.mark.asyncio
async def test_take_message(ctx: CallContext) -> None:
    result = await registry.dispatch(
        "take_message",
        {"caller_name": "Jane", "caller_phone": "555-1234", "message": "Call me back", "urgent": True},
        ctx,
    )
    assert result["status"] == "message_recorded"


@pytest.mark.asyncio
async def test_unknown_tool_name(ctx: CallContext) -> None:
    result = await registry.dispatch("delete_everything", {}, ctx)
    assert "error" in result


@pytest.mark.asyncio
async def test_end_call_sets_hangup_requested(ctx: CallContext) -> None:
    result = await registry.dispatch("end_call", {"reason": "done"}, ctx)
    assert result["status"] == "call_ending"
    assert ctx.hangup_requested is True


@pytest.mark.asyncio
async def test_transfer_call_without_mcp_falls_back_to_ws_transfer(ctx: CallContext) -> None:
    result = await registry.dispatch("transfer_call", {"target_number": "0771234567"}, ctx)
    assert result["status"] == "transfer_initiated_fallback"
    assert ctx.ws_transfer_target == "0771234567"
