"""Tool handlers the Gemini Live session can call mid-conversation. Each takes
the model's parsed function-call args plus the call's CallContext, and returns
a small JSON-serializable dict that becomes the tool's FunctionResponse.
"""
from __future__ import annotations

import difflib
import logging
import re
from datetime import datetime

import config
import languages
from data.json_store import BookingNotFoundError, SlotUnavailableError
from gemini_session.persona import normalize_language
from tools.context import CallContext
from tools.spoken import (branch_from_native, classify_reply, local_number, spoken_branch, spoken_date, spoken_number,
                          spoken_time)

logger = logging.getLogger(__name__)

_NOT_A_REAL_NUMBER = {"", "unknown", "browser simulation"}


def _salon_now() -> datetime:
    """Current time on the salon's clock (a seam so tests can pin it)."""
    return datetime.now(config.SALON_TZ).replace(tzinfo=None)


def _anything_else(ctx: CallContext) -> str:
    """The "anything else?" line for the call's language, as the tool results quote it."""
    lang = languages.get(normalize_language(ctx.language))
    return f'{lang.name}: "{lang.lines.anything_else}"'


def _parse_date(value: object) -> tuple[str | None, str | None]:
    """Returns (YYYY-MM-DD, None) or (None, error message)."""
    text = str(value or "").strip()
    try:
        return datetime.strptime(text, "%Y-%m-%d").strftime("%Y-%m-%d"), None
    except ValueError:
        return None, f"'{text}' is not a valid date; use YYYY-MM-DD (e.g. {_salon_now().strftime('%Y-%m-%d')})."


def _parse_time(value: object) -> tuple[str | None, str | None]:
    """Accepts H:MM, HH:MM, HH:MM:SS and h:mm AM/PM; returns (HH:MM, None) or (None, error)."""
    text = str(value or "").strip()
    for fmt in ("%H:%M", "%H:%M:%S", "%I:%M %p", "%I:%M%p", "%I %p", "%I%p"):
        try:
            return datetime.strptime(text.upper(), fmt).strftime("%H:%M"), None
        except ValueError:
            continue
    return None, f"'{text}' is not a valid time; use 24-hour HH:MM (e.g. 15:00)."


async def _find_service(name: object, ctx: CallContext):
    """Looks a service up forgivingly (case, spacing, small typos, unique
    substring). Returns (Service, None) or (None, error dict listing the real names)."""
    services = await ctx.store.list_services()
    wanted = " ".join(str(name or "").lower().split())
    by_norm = {" ".join(s.name.lower().split()): s for s in services}
    match = by_norm.get(wanted)
    if match is None and wanted:
        close = difflib.get_close_matches(wanted, list(by_norm), n=1, cutoff=0.75)
        if close:
            match = by_norm[close[0]]
        else:
            partial = [s for key, s in by_norm.items() if wanted in key or key in wanted]
            if len(partial) == 1:
                match = partial[0]
    if match is not None:
        return match, None
    return None, {
        "error": f"No service named '{name}' found.",
        "available_services": [s.name for s in services],
    }


async def _branches(ctx: CallContext) -> list[str]:
    if getattr(ctx, "settings_store", None) is None:
        return []
    try:
        return (await ctx.settings_store.get_settings()).branch_list
    except Exception:  # noqa: BLE001
        return []


async def _find_branch(name: object, ctx: CallContext) -> tuple[str, dict | None]:
    """Looks a branch up forgivingly. Returns (branch name, None) or ("", error dict listing the real branches).
    With no branches configured, or one branch and no name given, there is nothing to ask."""
    branches = await _branches(ctx)
    wanted = " ".join(str(name or "").lower().split())
    if not branches:
        return wanted and str(name).strip() or "", None
    if not wanted:
        if len(branches) == 1:
            return branches[0], None
        return "", {"error": "Ask the caller which branch they want.", "branches": branches}
    by_norm = {b.lower(): b for b in branches}
    native = branch_from_native(name)
    if native and native.lower() in by_norm:
        return by_norm[native.lower()], None
    if wanted in by_norm:
        return by_norm[wanted], None
    close = difflib.get_close_matches(wanted, list(by_norm), n=1, cutoff=0.7)
    if close:
        return by_norm[close[0]], None
    partial = [b for k, b in by_norm.items() if wanted in k or k in wanted]
    if len(partial) == 1:
        return partial[0], None
    return "", {"error": f"No branch named '{name}'.", "branches": branches}


def _price_fields(service) -> dict:
    if getattr(service, "price_varies", False):
        return {
            "price_lkr": None,
            "price_usd": None,
            "price_note": "The price depends on length and design; the team confirms it. Do NOT state a number.",
        }
    return {"price_lkr": service.price_usd, "price_usd": service.price_usd}


async def _open_slots(ctx: CallContext, service_name: str, date: str, branch: str = "") -> list[str]:
    """Free slots for the date, minus any that have already passed if it is today."""
    slots = await ctx.store.check_availability(service_name, date, branch)
    now = _salon_now()
    if date == now.strftime("%Y-%m-%d"):
        slots = [t for t in slots if t > now.strftime("%H:%M")]
    return slots


async def list_services(args: dict, ctx: CallContext) -> dict:
    services = await ctx.store.list_services()
    return {
        "currency": "LKR",
        "currency_name": "Sri Lankan Rupees (රුපියල්)",
        "services": [
            {
                "name": s.name,
                **_price_fields(s),
                "currency": "LKR",
                "duration_minutes": s.duration_minutes,
            }
            for s in services
        ],
    }


async def quote_price(args: dict, ctx: CallContext) -> dict:
    service, err = await _find_service(args.get("service_name"), ctx)
    if service is None:
        return err
    return {
        "name": service.name,
        **_price_fields(service),
        "currency": "LKR",
        "currency_name": "Sri Lankan Rupees (රුපියල්)",
        "duration_minutes": service.duration_minutes,
    }


async def check_availability(args: dict, ctx: CallContext) -> dict:
    service, err = await _find_service(args.get("service_name"), ctx)
    if service is None:
        return err
    date, date_err = _parse_date(args.get("date"))
    if date is None:
        return {"error": date_err}
    if date < _salon_now().strftime("%Y-%m-%d"):
        return {"error": f"{date} is in the past. Ask the caller for a date from today onwards."}
    branch, branch_err = await _find_branch(args.get("branch"), ctx)
    if branch_err:
        return branch_err
    slots = await _open_slots(ctx, service.name, date, branch)
    lang = normalize_language(ctx.language)
    return {"service_name": service.name, "branch": branch, "branch_say": spoken_branch(branch, lang), "date": date,
            "date_say": spoken_date(date, _salon_now().date(), lang), "available_times": slots,
            "times_say": {t: spoken_time(t, lang) for t in slots},
            "say": "Say the day only as date_say and each time only as in times_say, never as numerals, AM/PM or "
                   "\"පෙ.ව./ප.ව.\"."}


_REVIEW_KEYS = ("service_name", "date", "time", "branch", "customer_name", "customer_phone")


def _digits(value: object) -> str:
    return re.sub(r"\D", "", str(value or ""))


async def _validate_booking(args: dict, ctx: CallContext, existing=None) -> tuple[dict | None, dict | None]:
    """Checks booking details (merged over `existing` when correcting one). Returns (details, None) or
    (None, error dict)."""
    def pick(key: str, old: str = "") -> object:
        return args.get(key) or (getattr(existing, old or key, "") if existing else "")

    service, err = await _find_service(pick("service_name"), ctx)
    if service is None:
        return None, err
    date, date_err = _parse_date(pick("date"))
    if date is None:
        return None, {"error": date_err}
    time, time_err = _parse_time(pick("time"))
    if time is None:
        return None, {"error": time_err}
    if f"{date} {time}" <= _salon_now().strftime("%Y-%m-%d %H:%M"):
        return None, {"error": f"{date} {time} has already passed. Ask the caller for a later date or time."}

    customer_name = str(pick("customer_name") or "").strip()
    if not customer_name:
        return None, {"error": "Customer name is required. Ask the caller for their name."}
    phone = str(pick("customer_phone") or "").strip()
    caller = (ctx.caller_number or "").strip()
    if not phone and caller.lower() not in _NOT_A_REAL_NUMBER:
        phone = caller
    if not phone:
        return None, {"error": "A phone number is required. Ask the caller for their phone number."}
    if caller.lower() not in _NOT_A_REAL_NUMBER and _digits(phone) == _digits(caller):
        phone = local_number(phone) or phone  # the number they are calling from, as-is
    elif (normalized := local_number(phone)) is None:
        return None, {"error": f"'{phone}' is not a full phone number: a Sri Lankan number has 10 digits starting "
                               f"with 0 and this has {len(_digits(phone))}. Ask the caller to say the whole number "
                               "again and read it back."}
    else:
        phone = normalized

    branch, branch_err = await _find_branch(pick("branch") or None, ctx)
    if branch_err:
        return None, branch_err
    unchanged_slot = existing is not None and (existing.date, existing.time, existing.branch) == (date, time, branch)
    if not unchanged_slot:
        open_slots = await _open_slots(ctx, service.name, date, branch)
        if time not in open_slots:
            return None, {"error": f"{time} on {date} is not an available appointment time.",
                          "available_times": open_slots}
    notes = str(args.get("notes") or (existing.notes if existing else "") or "").strip()
    return {"service_name": service.name, "date": date, "time": time, "branch": branch,
            "customer_name": customer_name, "customer_phone": phone, "notes": notes}, None


def _spoken_details(details: dict, ctx: CallContext) -> dict:
    lang = normalize_language(ctx.language)
    return {
        **details,
        "branch_say": spoken_branch(details["branch"], lang),
        "phone_say": spoken_number(details["customer_phone"], lang),
        "date_say": spoken_date(details["date"], _salon_now().date(), lang),
        "time_say": spoken_time(details["time"], lang),
    }


async def review_booking(args: dict, ctx: CallContext) -> dict:
    """Step before create_booking/update_booking: validates the details and returns them in the form to read back.
    The booking is only saved after the caller has answered "yes" to that read-back."""
    booking_id = str(args.get("booking_id") or "").strip()
    existing = None
    if booking_id:
        existing = await ctx.store.get_booking(booking_id)
        if existing is None or booking_id not in ctx.booking_ids or existing.status != "requested":
            return {"error": "Only a booking request made in this call can be corrected. Use find_booking and "
                             "modify_booking for any other booking."}
    details, err = await _validate_booking(args, ctx, existing)
    if details is None:
        return err
    ctx.review = {"details": details, "booking_id": booking_id, "reply": "", "unclear": 0}
    return {
        **_spoken_details(details, ctx),
        "say": "Read these back to the caller exactly (the branch and the phone number ONLY in the *_say forms), "
               "ask if everything is correct, then STOP and wait for their answer. Do not save anything yet.",
    }


def _check_review(ctx: CallContext, details: dict, booking_id: str = "") -> dict | None:
    """None when the caller has said a clear "yes" to a read-back of exactly these details; else the error."""
    review = ctx.review
    same = review is not None and review["booking_id"] == booking_id and all(
        review["details"][k] == details[k] for k in _REVIEW_KEYS)
    if not same:
        return {"error": "Call review_booking with these exact details first, read them back and wait for the "
                         "caller's yes."}
    reply = review["reply"].strip()
    if not reply:
        return {"error": "The caller hasn't answered the read-back yet. Wait for their answer; do not save yet."}
    verdict = classify_reply(reply)
    if verdict == "no":
        ctx.review = None
        return {"error": f"The caller said something is wrong ('{reply}'). Do NOT save. Apologise, ask which detail "
                         "is wrong, take the correct value, repeat it back, then call review_booking again."}
    if verdict == "unclear" and review["unclear"] == 0:
        review["reply"], review["unclear"] = "", 1
        return {"error": f"That was not a clear yes (heard '{reply}'). Ask once more: is everything correct?"}
    ctx.review = None
    return None


async def create_booking(args: dict, ctx: CallContext) -> dict:
    details, err = await _validate_booking(args, ctx)
    if details is None:
        return err
    if (gate := _check_review(ctx, details)) is not None:
        return gate

    try:
        booking = await ctx.store.create_booking(**details)
    except SlotUnavailableError as exc:
        if ctx.call_manager and ctx.call_sid:
            await ctx.call_manager.record_action(ctx.call_sid, f"Slot unavailable for {details['service_name']}: {exc}")
        return {"error": str(exc), "available_times": await _open_slots(
            ctx, details["service_name"], details["date"], details["branch"])}
    ctx.booking_ids.add(booking.booking_id)

    if ctx.call_manager and ctx.call_sid:
        await ctx.call_manager.record_action(
            ctx.call_sid,
            f"Booking request: {booking.service_name} for {booking.customer_name} on {booking.date} at "
            f"{booking.time}" + (f" ({booking.branch})" if booking.branch else ""),
            booking.__dict__,
        )
        await ctx.call_manager.broadcast("booking_created", booking.__dict__)

    return {
        "booking_id": booking.booking_id,
        "service_name": booking.service_name,
        "branch": booking.branch,
        "date": booking.date,
        "time": booking.time,
        "status": booking.status,
        "say": "This is a booking REQUEST. Tell the caller it is recorded and the team will let them know once "
               "the slot is confirmed. Never say it is booked or confirmed. "
               f"Then, in the same turn, ask if they would like to know anything else ({_anything_else(ctx)}) "
               "and wait for their answer. "
               "If they then say a detail is wrong, fix it with review_booking (with this booking_id) and "
               "update_booking.",
    }


async def update_booking(args: dict, ctx: CallContext) -> dict:
    """Corrects a booking request made earlier in this same call, after a review_booking read-back and a yes."""
    booking_id = str(args.get("booking_id") or "").strip()
    existing = await ctx.store.get_booking(booking_id) if booking_id else None
    if existing is None or booking_id not in ctx.booking_ids or existing.status != "requested":
        return {"error": "Only a booking request made in this call, not yet confirmed by the team, can be "
                         "corrected. Use find_booking and modify_booking for any other booking."}
    details, err = await _validate_booking(args, ctx, existing)
    if details is None:
        return err
    if (gate := _check_review(ctx, details, booking_id)) is not None:
        return gate
    try:
        booking = await ctx.store.update_booking(booking_id, **details)
    except (BookingNotFoundError, SlotUnavailableError) as exc:
        return {"error": str(exc)}

    if ctx.call_manager and ctx.call_sid:
        await ctx.call_manager.record_action(
            ctx.call_sid,
            f"Booking request corrected: {booking.service_name} for {booking.customer_name} on {booking.date} at "
            f"{booking.time}" + (f" ({booking.branch})" if booking.branch else ""),
            booking.__dict__,
        )
        await ctx.call_manager.broadcast("booking_modified", booking.__dict__)
    return {
        "booking_id": booking.booking_id,
        "service_name": booking.service_name,
        "branch": booking.branch,
        "date": booking.date,
        "time": booking.time,
        "status": booking.status,
        "say": "The booking request is corrected. Tell the caller the corrected request is recorded and the team "
               "will confirm it. Never say it is booked or confirmed. "
               f"Then, in the same turn, ask if they would like to know anything else ({_anything_else(ctx)}) "
               "and wait for their answer.",
    }


async def find_booking(args: dict, ctx: CallContext) -> dict:
    """Look up the caller's active bookings (for a reschedule) by name, phone and/or date."""
    name = str(args.get("customer_name") or "").strip() or None
    phone = str(args.get("customer_phone") or "").strip()
    if not phone and (ctx.caller_number or "").strip().lower() not in _NOT_A_REAL_NUMBER and not name:
        phone = ctx.caller_number.strip()
    date = None
    if args.get("date"):
        date, date_err = _parse_date(args.get("date"))
        if date is None:
            return {"error": date_err}
    if not (name or phone):
        return {"error": "Ask the caller for the name the booking was made under."}
    found = await ctx.store.find_bookings(name=name, phone=phone or None, date=date)
    if not found:
        return {"bookings": [], "note": "No active booking found. Ask them to check the name and date, or offer a "
                                          "new booking request."}
    lang = normalize_language(ctx.language)
    today = _salon_now().date()
    return {"bookings": [
        {"booking_id": b.booking_id, "service_name": b.service_name, "branch": b.branch, "date": b.date,
         "time": b.time, "date_say": spoken_date(b.date, today, lang), "time_say": spoken_time(b.time, lang),
         "status": b.status, "customer_name": b.customer_name} for b in found
    ]}


async def modify_booking(args: dict, ctx: CallContext) -> dict:
    """A reschedule REQUEST: the booking keeps its old slot until staff approve the new one."""
    new_date, new_time = args.get("new_date"), args.get("new_time")
    if new_date:
        new_date, err = _parse_date(new_date)
        if new_date is None:
            return {"error": err}
    if new_time:
        new_time, err = _parse_time(new_time)
        if new_time is None:
            return {"error": err}
    if not (new_date or new_time):
        return {"error": "Ask the caller for the new date and time."}
    lang = normalize_language(ctx.language)
    existing = await ctx.store.get_booking(args["booking_id"])
    if existing is None or not existing.active:
        return {"error": f"No active booking with id {args['booking_id']}"}
    check_date, check_time = new_date or existing.date, new_time or existing.time
    if f"{check_date} {check_time}" <= _salon_now().strftime("%Y-%m-%d %H:%M"):
        return {"error": f"{check_date} {check_time} has already passed. Ask for a later date or time."}
    try:
        booking = await ctx.store.request_change(args["booking_id"], date=new_date, time=new_time)
    except BookingNotFoundError as exc:
        return {"error": str(exc)}
    except SlotUnavailableError as exc:
        return {"error": str(exc), "available_times": await _open_slots(ctx, existing.service_name, check_date,
                                                                          existing.branch)}

    if ctx.call_manager and ctx.call_sid:
        await ctx.call_manager.record_action(
            ctx.call_sid,
            f"Reschedule request for booking {booking.booking_id}: {booking.requested_date} {booking.requested_time}",
            booking.__dict__,
        )
        await ctx.call_manager.broadcast("booking_modified", booking.__dict__)

    return {
        "booking_id": booking.booking_id,
        "status": "change_requested",
        "current_date": booking.date,
        "current_time": booking.time,
        "requested_date": booking.requested_date,
        "requested_time": booking.requested_time,
        "requested_date_say": spoken_date(booking.requested_date, _salon_now().date(), lang)
        if booking.requested_date else "",
        "requested_time_say": spoken_time(booking.requested_time, lang) if booking.requested_time else "",
        "say": "Say days and times only in the *_say forms. Tell the caller the change is requested and the team will confirm the new time; until then the "
               "old appointment is NOT cancelled.",
    }


async def cancel_booking(args: dict, ctx: CallContext) -> dict:
    try:
        await ctx.store.cancel_booking(args["booking_id"])
    except BookingNotFoundError as exc:
        return {"error": str(exc)}

    if ctx.call_manager and ctx.call_sid:
        await ctx.call_manager.record_action(
            ctx.call_sid,
            f"Cancelled booking {args['booking_id']}",
            {"booking_id": args["booking_id"]},
        )
        await ctx.call_manager.broadcast("booking_cancelled", {"booking_id": args["booking_id"]})

    return {"booking_id": args["booking_id"], "status": "cancelled"}


MESSAGE_CATEGORIES = ("general", "complaint", "price_inquiry", "treatment_inquiry", "location_link", "callback")


async def take_message(args: dict, ctx: CallContext) -> dict:
    category = args.get("category") if args.get("category") in MESSAGE_CATEGORIES else "general"
    urgent = bool(args.get("urgent", False)) or category == "complaint"
    phone = args.get("caller_phone") or (
        ctx.caller_number if (ctx.caller_number or "").strip().lower() not in _NOT_A_REAL_NUMBER else None)
    branch, _ = await _find_branch(args.get("branch"), ctx) if args.get("branch") else ("", None)
    await ctx.store.take_message(
        caller_name=args.get("caller_name"),
        caller_phone=phone,
        message=args["message"],
        urgent=urgent,
        category=category,
        branch=branch,
    )
    if ctx.call_manager and ctx.call_sid:
        await ctx.call_manager.record_action(
            ctx.call_sid,
            f"Message ({category}) from {args.get('caller_name') or 'Caller'}: {args['message'][:50]}...",
            {"urgent": urgent, "category": category},
        )
        await ctx.call_manager.broadcast("message_taken", {
            "caller_name": args.get("caller_name"),
            "caller_phone": phone,
            "message": args["message"],
            "urgent": urgent,
            "category": category,
            "branch": branch,
        })
    return {"status": "message_recorded"}


async def transfer_call(args: dict, ctx: CallContext) -> dict:
    """Transfer the live call to a human. Tries the MCP transfer_call tool
    first; if that fails (or no MCP client is configured), falls back to the
    WS-level transfer event, which call_session.py sends after this returns."""
    target = args.get("target_number")
    if not target and getattr(ctx, "settings_store", None):
        try:
            s = await ctx.settings_store.get_settings()
            target = s.transfer_phone
        except Exception:
            target = "+94777412853"
    elif not target:
        target = "+94777412853"
    reason = args.get("reason", "caller requested transfer")

    if ctx.call_manager and ctx.call_sid:
        await ctx.call_manager.record_action(
            ctx.call_sid,
            f"Transfer requested to {target} ({reason})",
            {"target": target, "reason": reason},
        )

    if ctx.mcp is not None and ctx.call_sid:
        try:
            await ctx.mcp.transfer_call(ctx.call_sid, ctx.other_leg_call_id, ctx.account_id, target, reason)
            return {"status": "transfer_initiated", "target": target}
        except Exception:  # noqa: BLE001 - fall back to the WS-level transfer event
            logger.exception("MCP transfer_call failed; falling back to WS transfer event")

    ctx.ws_transfer_target = target
    return {"status": "transfer_initiated_fallback", "target": target}


async def end_call(args: dict, ctx: CallContext) -> dict:
    """End the live call. Best-effort MCP hangup, then always signal
    call_session.py to close the connection regardless of MCP outcome."""
    reason = args.get("reason", "completed")
    if ctx.asked_anything_else and "no_response" not in str(reason):
        ctx.asked_anything_else = False
        return {"error": "You just asked if they need anything else. Do not hang up yet: wait for their answer, "
                         "and call end_call only after they say no or goodbye."}

    if ctx.call_manager and ctx.call_sid:
        await ctx.call_manager.record_action(
            ctx.call_sid,
            f"Hangup requested: {reason}",
            {"reason": reason},
        )

    if ctx.mcp is not None and ctx.call_sid:
        try:
            await ctx.mcp.hangup_call(ctx.call_sid, ctx.other_leg_call_id, ctx.account_id, reason)
        except Exception:  # noqa: BLE001 - hang up our side regardless
            logger.exception("MCP hangup_call failed")

    ctx.hangup_requested = True
    return {"status": "call_ending"}
