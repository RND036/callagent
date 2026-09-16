"""WebSocket server SmartPBX connects to. One connection == one phone call;
each gets its own CallSession running for the connection's lifetime.

SmartPBX dials us as a WS client per call -- we run the server side.
"""
from __future__ import annotations

import logging

import websockets
from google import genai

from data.store import BookingStore
from pbx_bridge.call_session import CallSession
from pbx_bridge.mcp_client import McpClient

logger = logging.getLogger(__name__)


def make_handler(gemini_client: genai.Client, model: str, store: BookingStore, mcp_client: McpClient | None):
    async def handler(pbx_ws) -> None:
        logger.info("PBX connection opened: %s", pbx_ws.remote_address)
        session = CallSession(pbx_ws, gemini_client, model, store, mcp_client)
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
):
    handler = make_handler(gemini_client, model, store, mcp_client)
    return await websockets.serve(handler, host, port)
