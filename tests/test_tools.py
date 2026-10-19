import shutil
from datetime import datetime, timedelta
from pathlib import Path

import pytest

import config
from data.json_store import JsonBookingStore
from tools import handlers, registry
from tools.context import CallContext


FUTURE_DATE = (datetime.now() + timedelta(days=30)).strftime("%Y-%m-%d")


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
    from data.settings_store import SettingsStore

    return CallContext(
        store=store,
        settings_store=SettingsStore(settings_path=tmp_path / "settings.json"),
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
        "check_availability", {"service_name": "Manicure", "date": FUTURE_DATE, "branch": "Nugegoda"}, ctx
    )
    assert result["available_times"]
    assert "09:00" in result["available_times"]


BOOKING = {
    "service_name": "Manicure",
    "date": FUTURE_DATE,
    "time": "09:00",
    "branch": "Nugegoda",
    "customer_name": "Jane Doe",
    "customer_phone": "0771234567",
}


async def book(ctx, args=None, reply="ඔව්"):
    """review_booking, the caller's answer, then create_booking: the order the agent must follow."""
    args = args or BOOKING
    reviewed = await registry.dispatch("review_booking", args, ctx)
    if "error" in reviewed:
        return reviewed
    ctx.caller_said(reply)
    return await registry.dispatch("create_booking", args, ctx)


@pytest.mark.asyncio
async def test_create_booking_is_a_request_that_holds_the_slot_per_branch(ctx: CallContext) -> None:
    result = await book(ctx)
    assert result["status"] == "requested"
    assert "Never say it is booked or confirmed" in result["say"]
    booking_id = result["booking_id"]

    nugegoda = await registry.dispatch(
        "check_availability", {"service_name": "Manicure", "date": FUTURE_DATE, "branch": "Nugegoda"}, ctx)
    hokandara = await registry.dispatch(
        "check_availability", {"service_name": "Manicure", "date": FUTURE_DATE, "branch": "Hokandara"}, ctx)
    assert "09:00" not in nugegoda["available_times"]
    assert "09:00" in hokandara["available_times"]  # the other branch is still free

    assert "error" in await book(ctx)  # same slot, same branch

    cancelled = await registry.dispatch("cancel_booking", {"booking_id": booking_id}, ctx)
    assert cancelled["status"] == "cancelled"


@pytest.mark.asyncio
async def test_branch_is_required_and_matched_forgivingly(ctx: CallContext) -> None:
    bad = await registry.dispatch("check_availability", {"service_name": "Manicure", "date": FUTURE_DATE}, ctx)
    assert "branches" in bad and "Hokandara" in bad["branches"]
    typo = await registry.dispatch(
        "check_availability", {"service_name": "Manicure", "date": FUTURE_DATE, "branch": "nugegoda "}, ctx)
    assert typo["branch"] == "Nugegoda"
    unknown = await registry.dispatch(
        "check_availability", {"service_name": "Manicure", "date": FUTURE_DATE, "branch": "Kandy"}, ctx)
    assert "error" in unknown


@pytest.mark.asyncio
async def test_find_booking_and_reschedule_is_only_a_request(ctx: CallContext) -> None:
    booking_id = (await book(ctx))["booking_id"]

    found = await registry.dispatch("find_booking", {"customer_name": "jane doe", "date": FUTURE_DATE}, ctx)
    assert [b["booking_id"] for b in found["bookings"]] == [booking_id]
    assert (await registry.dispatch("find_booking", {"customer_name": "Someone Else"}, ctx))["bookings"] == []

    changed = await registry.dispatch("modify_booking", {"booking_id": booking_id, "new_time": "10:00"}, ctx)
    assert changed["status"] == "change_requested"
    assert changed["current_time"] == "09:00" and changed["requested_time"] == "10:00"
    assert "NOT cancelled" in changed["say"]

    booking = await ctx.store.get_booking(booking_id)
    assert (booking.time, booking.status, booking.requested_time) == ("09:00", "requested", "10:00")
    slots = await registry.dispatch(
        "check_availability", {"service_name": "Manicure", "date": FUTURE_DATE, "branch": "Nugegoda"}, ctx)
    assert "09:00" not in slots["available_times"] and "10:00" not in slots["available_times"]

    approved = await ctx.store.approve_change(booking_id)
    assert (approved.time, approved.requested_time) == ("10:00", "")


@pytest.mark.asyncio
async def test_staff_confirm_and_decline(ctx: CallContext) -> None:
    booking_id = (await book(ctx))["booking_id"]
    assert (await ctx.store.set_status(booking_id, "confirmed")).status == "confirmed"
    assert (await ctx.store.set_status(booking_id, "declined")).status == "declined"
    free = await registry.dispatch(
        "check_availability", {"service_name": "Manicure", "date": FUTURE_DATE, "branch": "Nugegoda"}, ctx)
    assert "09:00" in free["available_times"]  # a declined request frees the slot


@pytest.mark.asyncio
async def test_price_varies_service_never_gets_a_number(ctx: CallContext) -> None:
    from data.store import Service

    await ctx.store.upsert_service(Service("Gel extensions", 9000.0, 90, price_varies=True))
    quote = await registry.dispatch("quote_price", {"service_name": "Gel extensions"}, ctx)
    assert quote["price_lkr"] is None and "Do NOT state a number" in quote["price_note"]
    listed = await registry.dispatch("list_services", {}, ctx)
    assert next(s for s in listed["services"] if s["name"] == "Gel extensions")["price_lkr"] is None
    fixed = await registry.dispatch("quote_price", {"service_name": "Manicure"}, ctx)
    assert fixed["price_lkr"] == 2500.0


@pytest.mark.asyncio
async def test_complaints_are_urgent_and_categorised(ctx: CallContext) -> None:
    await registry.dispatch("take_message", {
        "caller_name": "Shashini", "message": "Gel polish lifting after 3 days", "category": "complaint",
        "branch": "nugegoda"}, ctx)
    await registry.dispatch("take_message", {"message": "price for gel extensions", "category": "price_inquiry"}, ctx)
    complaint, inquiry = await ctx.store.list_messages()
    assert complaint["urgent"] is True and complaint["category"] == "complaint"
    assert complaint["branch"] == "Nugegoda" and complaint["status"] == "open" and complaint["message_id"]
    assert inquiry["category"] == "price_inquiry" and inquiry["urgent"] is False
    assert inquiry["caller_phone"] == "555-1234"  # defaults to the caller ID
    assert await ctx.store.mark_message_done(complaint["message_id"])
    assert (await ctx.store.list_messages())[0]["status"] == "done"


@pytest.mark.asyncio
async def test_old_data_files_without_the_new_fields_still_load(tmp_path: Path) -> None:
    import json

    (tmp_path / "s.json").write_text(json.dumps([{"name": "Manicure", "price_usd": 2500.0, "duration_minutes": 30}]))
    (tmp_path / "b.json").write_text(json.dumps([{
        "booking_id": "b-1", "service_name": "Manicure", "date": FUTURE_DATE, "time": "09:00",
        "customer_name": "Old", "customer_phone": "1", "status": "confirmed"}]))
    (tmp_path / "m.json").write_text(json.dumps([{"caller_name": "x", "message": "hi", "urgent": False}]))
    store = JsonBookingStore(tmp_path / "s.json", tmp_path / "b.json", tmp_path / "m.json", "09:00", "19:00", 30)
    assert (await store.list_services())[0].price_varies is False
    [booking] = await store.list_bookings()
    assert booking.branch == "" and booking.status == "confirmed"
    # an old booking with no branch blocks that slot at every branch
    assert "09:00" not in await store.check_availability("Manicure", FUTURE_DATE, "Hokandara")
    assert (await store.list_messages())[0]["message"] == "hi"


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


@pytest.mark.asyncio
async def test_check_availability_rejects_past_date(ctx: CallContext) -> None:
    result = await registry.dispatch("check_availability", {"service_name": "Manicure", "date": "2020-01-01"}, ctx)
    assert "past" in result["error"]


@pytest.mark.asyncio
async def test_check_availability_hides_slots_that_already_passed_today(ctx: CallContext, monkeypatch) -> None:
    monkeypatch.setattr(handlers, "_salon_now", lambda: datetime(2030, 5, 6, 14, 5))
    result = await registry.dispatch("check_availability", {"service_name": "Manicure", "date": "2030-05-06", "branch": "Nugegoda"}, ctx)
    assert "09:00" not in result["available_times"]
    assert "14:00" not in result["available_times"]
    assert "14:30" in result["available_times"]


@pytest.mark.asyncio
async def test_service_name_is_matched_forgivingly(ctx: CallContext) -> None:
    result = await registry.dispatch("quote_price", {"service_name": "  Gel Manicures "}, ctx)
    assert result["name"] == "Gel manicure"

    missing = await registry.dispatch("quote_price", {"service_name": "Hair cut"}, ctx)
    assert "Manicure" in missing["available_services"]


@pytest.mark.asyncio
async def test_create_booking_defaults_phone_to_caller_id(ctx: CallContext) -> None:
    args = {"service_name": "Manicure", "date": FUTURE_DATE, "time": "3:00 PM", "customer_name": "Nimali",
            "branch": "Nugegoda"}
    result = await book(ctx, args)
    assert result["time"] == "15:00"
    booking = await ctx.store.get_booking(result["booking_id"])
    assert booking.customer_phone == "555-1234"


@pytest.mark.asyncio
async def test_create_booking_needs_a_phone_when_caller_id_unknown(ctx: CallContext) -> None:
    ctx.caller_number = "Unknown"
    args = {"service_name": "Manicure", "date": FUTURE_DATE, "time": "10:00", "customer_name": "Nimali"}
    result = await registry.dispatch("create_booking", args, ctx)
    assert "phone" in result["error"]


@pytest.mark.asyncio
async def test_create_booking_rejects_past_and_off_grid_times(ctx: CallContext) -> None:
    base = {"service_name": "Manicure", "customer_name": "Nimali", "customer_phone": "0771234567", "branch": "Nugegoda"}
    past = await registry.dispatch("create_booking", {**base, "date": "2020-01-01", "time": "10:00"}, ctx)
    assert "passed" in past["error"]

    off_grid = await registry.dispatch("review_booking", {**base, "date": FUTURE_DATE, "time": "10:10"}, ctx)
    assert "09:00" in off_grid["available_times"]

    bad_date = await registry.dispatch("create_booking", {**base, "date": "tomorrow", "time": "10:00"}, ctx)
    assert "YYYY-MM-DD" in bad_date["error"]


@pytest.mark.asyncio
async def test_create_needs_a_review_and_the_callers_answer(ctx: CallContext) -> None:
    assert "review_booking" in (await registry.dispatch("create_booking", BOOKING, ctx))["error"]
    await registry.dispatch("review_booking", BOOKING, ctx)
    assert "hasn't answered" in (await registry.dispatch("create_booking", BOOKING, ctx))["error"]  # same turn
    ctx.caller_said("ඔව්")
    assert (await registry.dispatch("create_booking", BOOKING, ctx))["status"] == "requested"
    assert ctx.review is None


@pytest.mark.asyncio
async def test_a_no_blocks_the_save_and_clears_the_review(ctx: CallContext) -> None:
    result = await book(ctx, reply="නැහැ, අංකය වැරදියි")
    assert "Do NOT save" in result["error"] and ctx.review is None
    assert await ctx.store.list_bookings() == []


@pytest.mark.asyncio
async def test_unclear_answer_is_asked_once_more(ctx: CallContext) -> None:
    assert "not a clear yes" in (await book(ctx, reply="ඔක්කොම"))["error"]
    ctx.caller_said("ඔව් හරි")
    assert (await registry.dispatch("create_booking", BOOKING, ctx))["status"] == "requested"


@pytest.mark.asyncio
async def test_no_problem_counts_as_yes(ctx: CallContext) -> None:
    assert (await book(ctx, reply="ප්‍රශ්නයක් නෑ"))["status"] == "requested"


@pytest.mark.asyncio
async def test_details_changed_after_the_review_are_refused(ctx: CallContext) -> None:
    await registry.dispatch("review_booking", BOOKING, ctx)
    ctx.caller_said("ඔව්")
    changed = await registry.dispatch("create_booking", {**BOOKING, "time": "10:00"}, ctx)
    assert "review_booking" in changed["error"]


@pytest.mark.asyncio
async def test_short_phone_number_is_refused(ctx: CallContext) -> None:
    result = await registry.dispatch("review_booking", {**BOOKING, "customer_phone": "078 182 774"}, ctx)
    assert "10 digits" in result["error"]


@pytest.mark.asyncio
async def test_review_returns_spoken_forms(ctx: CallContext) -> None:
    result = await registry.dispatch("review_booking", {**BOOKING, "customer_phone": "+94781827743"}, ctx)
    assert result["customer_phone"] == "0781827743"
    assert result["phone_say"] == "බිංදුව හැත්තෑ අට, එකසිය අසූ දෙක, හැත්තෑ හත, හතළිස් තුන"
    assert result["time_say"] and "පෙ.ව" not in result["time_say"] and "AM" not in result["time_say"]
    assert result["branch_say"] == "නුගේගොඩ"


@pytest.mark.asyncio
async def test_native_branch_name_matches(ctx: CallContext) -> None:
    result = await registry.dispatch(
        "check_availability", {"service_name": "Manicure", "date": FUTURE_DATE, "branch": "නුගේගොඩ"}, ctx)
    assert result["branch"] == "Nugegoda"


@pytest.mark.asyncio
async def test_update_booking_corrects_phone_branch_and_time(ctx: CallContext) -> None:
    booking_id = (await book(ctx))["booking_id"]
    fix = {**BOOKING, "booking_id": booking_id, "customer_phone": "0781827743", "branch": "Hokandara",
           "time": "10:00"}
    assert "review_booking" in (await registry.dispatch("update_booking", fix, ctx))["error"]
    await registry.dispatch("review_booking", fix, ctx)
    ctx.caller_said("ඔව්")
    updated = await registry.dispatch("update_booking", fix, ctx)
    assert (updated["branch"], updated["time"]) == ("Hokandara", "10:00")
    booking = await ctx.store.get_booking(booking_id)
    assert booking.customer_phone == "0781827743"
    old = await registry.dispatch(
        "check_availability", {"service_name": "Manicure", "date": FUTURE_DATE, "branch": "Nugegoda"}, ctx)
    assert "09:00" in old["available_times"]  # the old slot is free again


@pytest.mark.asyncio
async def test_update_booking_refuses_a_booking_from_another_call(ctx: CallContext) -> None:
    created = await ctx.store.create_booking("Manicure", FUTURE_DATE, "11:00", "Old", "0771111111", "Nugegoda")
    for tool in ("review_booking", "update_booking"):
        result = await registry.dispatch(tool, {**BOOKING, "booking_id": created.booking_id}, ctx)
        assert "made in this call" in result["error"]


@pytest.mark.asyncio
async def test_booking_replies_tell_the_agent_to_ask_anything_else(ctx: CallContext) -> None:
    created = await book(ctx)
    assert "ඔබට වෙනත් යමක් දැනගැනීමට අවශ්‍යද" in created["say"]
    fix = {**BOOKING, "booking_id": created["booking_id"], "time": "10:00"}
    await registry.dispatch("review_booking", fix, ctx)
    ctx.caller_said("ඔව්")
    updated = await registry.dispatch("update_booking", fix, ctx)
    assert "ඔබට වෙනත් යමක් දැනගැනීමට අවශ්‍යද" in updated["say"]


@pytest.mark.asyncio
async def test_check_availability_returns_spoken_day_and_times(ctx: CallContext, monkeypatch) -> None:
    monkeypatch.setattr(handlers, "_salon_now", lambda: datetime(2030, 5, 6, 8, 0))  # a Monday
    result = await registry.dispatch(
        "check_availability", {"service_name": "Manicure", "date": "2030-05-13", "branch": "Nugegoda"}, ctx)
    assert result["date_say"] == "ලබන සතියේ සඳුදා, මැයි දහතුන්වෙනිදා"
    assert result["times_say"]["09:00"] == "උදේ නවයට"
