"""Tool handlers the Gemini Live session can call mid-conversation. Each takes
the model's parsed function-call args plus the call's CallContext, and returns
a small JSON-serializable dict that becomes the tool's FunctionResponse.
"""
from __future__ import annotations

import logging

from data.json_store import BookingNotFoundError, SlotUnavailableError
from tools.context import CallContext

logger = logging.getLogger(__name__)


async def list_services(args: dict, ctx: CallContext) -> dict:
    services = await ctx.store.list_services()
    return {
        "services": [
            {"name": s.name, "price_usd": s.price_usd, "duration_minutes": s.duration_minutes}
            for s in services
        ]
    }


async def quote_price(args: dict, ctx: CallContext) -> dict:
    service_name = args["service_name"]
    service = await ctx.store.get_service(service_name)
    if service is None:
        return {"error": f"No service named '{service_name}' found."}
    return {
        "name": service.name,
        "price_usd": service.price_usd,
        "duration_minutes": service.duration_minutes,
    }


async def check_availability(args: dict, ctx: CallContext) -> dict:
    service_name = args["service_name"]
    date = args["date"]
    service = await ctx.store.get_service(service_name)
    if service is None:
        return {"error": f"No service named '{service_name}' found."}
    slots = await ctx.store.check_availability(service_name, date)
    return {"service_name": service.name, "date": date, "available_times": slots}


async def create_booking(args: dict, ctx: CallContext) -> dict:
    service_name = args["service_name"]
    service = await ctx.store.get_service(service_name)
    if service is None:
        return {"error": f"No service named '{service_name}' found."}
    try:
        booking = await ctx.store.create_booking(
            service_name=service_name,
            date=args["date"],
            time=args["time"],
            customer_name=args["customer_name"],
            customer_phone=args["customer_phone"],
        )
    except SlotUnavailableError as exc:
        return {"error": str(exc)}
    return {
        "booking_id": booking.booking_id,
        "service_name": booking.service_name,
        "date": booking.date,
        "time": booking.time,
        "status": booking.status,
    }


async def modify_booking(args: dict, ctx: CallContext) -> dict:
    try:
        booking = await ctx.store.modify_booking(
            booking_id=args["booking_id"],
            date=args.get("new_date"),
            time=args.get("new_time"),
        )
    except BookingNotFoundError as exc:
        return {"error": str(exc)}
    return {
        "booking_id": booking.booking_id,
        "service_name": booking.service_name,
        "date": booking.date,
        "time": booking.time,
        "status": booking.status,
    }


async def cancel_booking(args: dict, ctx: CallContext) -> dict:
    try:
        await ctx.store.cancel_booking(args["booking_id"])
    except BookingNotFoundError as exc:
        return {"error": str(exc)}
    return {"booking_id": args["booking_id"], "status": "cancelled"}


async def take_message(args: dict, ctx: CallContext) -> dict:
    await ctx.store.take_message(
        caller_name=args.get("caller_name"),
        caller_phone=args.get("caller_phone"),
        message=args["message"],
        urgent=args.get("urgent", False),
    )
    return {"status": "message_recorded"}


async def transfer_call(args: dict, ctx: CallContext) -> dict:
    """Transfer the live call to a human. Tries the MCP transfer_call tool
    first; if that fails (or no MCP client is configured), falls back to the
    WS-level transfer event, which call_session.py sends after this returns."""
    target = args["target_number"]
    reason = args.get("reason", "caller requested transfer")

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

    if ctx.mcp is not None and ctx.call_sid:
        try:
            await ctx.mcp.hangup_call(ctx.call_sid, ctx.other_leg_call_id, ctx.account_id, reason)
        except Exception:  # noqa: BLE001 - hang up our side regardless
            logger.exception("MCP hangup_call failed")

    ctx.hangup_requested = True
    return {"status": "call_ending"}
