"""Per-call context handed to every tool handler: the booking store plus
everything a handler needs to act on the live call (transfer/hangup via MCP),
and what the call has established about the caller so far.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from data.store import BookingStore
from pbx_bridge.mcp_client import McpClient

if TYPE_CHECKING:
    from data.call_manager import CallManager


@dataclass
class CallContext:
    store: BookingStore
    mcp: McpClient | None
    call_sid: str | None
    other_leg_call_id: str | None
    account_id: str | None
    caller_number: str | None
    hangup_requested: bool = False
    ws_transfer_target: str | None = None
    call_manager: CallManager | None = None
    settings_store: Any = None
    faq_store: Any = None
    language: str = "si"  # picked by the caller in the IVR menu: si / ta / en
    language_menu_key: str | None = None  # pressed mid-call, reopens the language menu; None: there is no menu to play
    end_reason: str = ""
    said_goodbye: bool = False  # the agent already said goodbye before calling end_call
    asked_anything_else: bool = False  # the agent asked "anything else?" in the turn that called the tool
    ended_event: asyncio.Event = field(default_factory=asyncio.Event)  # set once end_call has run
    # What the tools established about the caller (name, service, booking...): survives a Live reconnect and is
    # saved on the call record. Keys: name, phone, service, branch, date, time, booking_id, booking_status, message.
    customer: dict[str, str] = field(default_factory=dict)
    # The booking details last read back to the caller, and what the caller said since (see tools/handlers.py).
    review: dict | None = None
    booking_ids: set[str] = field(default_factory=set)  # bookings created in this call: the only ones it may edit

    def caller_said(self, text: str) -> None:
        """Collect the caller's words while a read-back is waiting for its answer."""
        if not (text and text.strip()):
            return
        self.asked_anything_else = False  # the caller has answered
        if self.review is not None:
            self.review["reply"] = (self.review.get("reply", "") + " " + text).strip()

    @property
    def winding_down(self) -> bool:
        """The call is ending: nothing may cut in or restart it."""
        return self.hangup_requested

    def remember(self, tool: str, args: dict, result: Any) -> bool:
        """Keep what a tool call just established about the caller in `customer`. Failed calls (an "error" in the
        result) teach nothing. Returns whether anything changed."""
        if not isinstance(result, dict) or result.get("error"):
            return False
        found: dict[str, Any] = {}
        if tool in ("create_booking", "update_booking"):
            found = {"name": args.get("customer_name"), "phone": args.get("customer_phone"),
                     "service": result.get("service_name"), "branch": result.get("branch"),
                     "date": result.get("date"), "time": result.get("time"),
                     "booking_id": result.get("booking_id"), "booking_status": result.get("status")}
        elif tool == "modify_booking":
            found = {"booking_id": result.get("booking_id"), "booking_status": result.get("status"),
                     "date": result.get("requested_date"), "time": result.get("requested_time")}
        elif tool == "cancel_booking":
            found = {"booking_id": result.get("booking_id"), "booking_status": "cancelled"}
        elif tool == "find_booking":
            found = {"name": args.get("customer_name"), "phone": args.get("customer_phone")}
            bookings = result.get("bookings") or []
            if len(bookings) == 1:
                b = bookings[0]
                found.update(name=b.get("customer_name"), service=b.get("service_name"), branch=b.get("branch"),
                             date=b.get("date"), time=b.get("time"), booking_id=b.get("booking_id"),
                             booking_status=b.get("status"))
        elif tool == "check_availability":
            found = {"service": result.get("service_name"), "branch": result.get("branch"),
                     "date": result.get("date")}
        elif tool == "quote_price":
            found = {"service": result.get("name")}
        elif tool == "take_message":
            found = {"name": args.get("caller_name"), "phone": args.get("caller_phone"),
                     "message": str(args.get("message") or "")[:160]}
        changed = False
        for key, value in found.items():
            value = str(value or "").strip()
            if value and self.customer.get(key) != value:
                self.customer[key] = value
                changed = True
        return changed
