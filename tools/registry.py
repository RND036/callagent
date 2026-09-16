"""Dispatch table from a Gemini tool_call's function name to the local handler."""
from __future__ import annotations

import logging

from tools import handlers
from tools.context import CallContext

logger = logging.getLogger(__name__)

_HANDLERS = {
    "list_services": handlers.list_services,
    "quote_price": handlers.quote_price,
    "check_availability": handlers.check_availability,
    "create_booking": handlers.create_booking,
    "modify_booking": handlers.modify_booking,
    "cancel_booking": handlers.cancel_booking,
    "take_message": handlers.take_message,
    "transfer_call": handlers.transfer_call,
    "end_call": handlers.end_call,
}


async def dispatch(name: str, args: dict, ctx: CallContext) -> dict:
    handler = _HANDLERS.get(name)
    if handler is None:
        logger.warning("Unknown tool call: %s", name)
        return {"error": f"Unknown tool '{name}'"}
    try:
        return await handler(args, ctx)
    except Exception as exc:  # noqa: BLE001 - must not crash the call over a tool bug
        logger.exception("Tool '%s' raised an exception", name)
        return {"error": f"Internal error running '{name}': {exc}"}
