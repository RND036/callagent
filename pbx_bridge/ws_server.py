"""WebSocket server FreeSWITCH's mod_audio_stream connects to. One connection
== one phone call; each gets its own CallSession running for the
connection's lifetime.

FreeSWITCH dials us as a WS client per call (via the dialplan's
uuid_audio_stream action) -- we run the server side.
"""
from __future__ import annotations

import logging
from typing import Any

import websockets
from google import genai

from data.call_manager import CallManager
from data.store import BookingStore
from pbx_bridge.call_session import CallSession
from pbx_bridge.mcp_client import McpClient

logger = logging.getLogger(__name__)


def make_handler(
    gemini_client: genai.Client,
    model: str,
    store: BookingStore,
    mcp_client: McpClient | None,
    call_manager: CallManager | None = None,
    settings_store: Any = None,
):
    async def handler(pbx_ws) -> None:
        logger.info("PBX connection opened: %s", pbx_ws.remote_address)
        session = CallSession(pbx_ws, gemini_client, model, store, mcp_client, call_manager, settings_store)
        try:
            await session.run()
        finally:
            logger.info("PBX connection closed: %s", pbx_ws.remote_address)

    return handler


async def serve(
    host: str,
    port: int,
    gemini_client: genai.Client,
    model: str,
    store: BookingStore,
    mcp_client: McpClient | None = None,
    call_manager: CallManager | None = None,
    settings_store: Any = None,
):
    handler = make_handler(gemini_client, model, store, mcp_client, call_manager, settings_store)
    return await websockets.serve(handler, host, port)
