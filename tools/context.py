"""Per-call context handed to every tool handler: the booking store plus
everything a handler needs to act on the live call (transfer/hangup via MCP).
"""
from __future__ import annotations

from dataclasses import dataclass

from data.store import BookingStore
from pbx_bridge.mcp_client import McpClient

from typing import TYPE_CHECKING
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
