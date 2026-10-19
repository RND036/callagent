"""TCP server Asterisk's AudioSocket() dialplan application connects to. One
connection == one phone call; each gets its own CallSession running for the
connection's lifetime.

Asterisk dials us as a TCP client per call (via the dialplan's
AudioSocket(uuid, host:port) action) -- we run the server side, same
connection direction as the FreeSWITCH WS bridge it replaces, just over a
raw TCP socket with TLV framing instead of WebSocket message framing.
"""
from __future__ import annotations

import asyncio
import logging
import struct
from typing import Any

from google import genai

from data.call_manager import CallManager
from data.store import BookingStore
from pbx_bridge.call_session import CallSession
from pbx_bridge.mcp_client import McpClient

logger = logging.getLogger(__name__)

_HEADER_STRUCT = struct.Struct(">BH")  # 1-byte type, 2-byte big-endian length


class AudioSocketConnection:
    """Thin wrapper around an asyncio TCP stream exposing the minimal
    recv_packet/send_packet/close interface CallSession needs, in place of
    the `websockets` connection object the FreeSWITCH adapter used."""

    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self._reader = reader
        self._writer = writer
        peer = writer.get_extra_info("peername")
        self.remote_address = peer

    async def recv_packet(self) -> tuple[int, bytes] | None:
        try:
            header = await self._reader.readexactly(_HEADER_STRUCT.size)
        except (asyncio.IncompleteReadError, ConnectionError):
            return None
        msg_type, length = _HEADER_STRUCT.unpack(header)
        if length == 0:
            return msg_type, b""
        try:
            payload = await self._reader.readexactly(length)
        except (asyncio.IncompleteReadError, ConnectionError):
            return None
        return msg_type, payload

    async def send_packet(self, msg_type: int, payload: bytes) -> None:
        self._writer.write(_HEADER_STRUCT.pack(msg_type, len(payload)) + payload)
        try:
            await self._writer.drain()
        except ConnectionError:
            pass  # socket may already be closed; let the reader loop notice

    async def close(self) -> None:
        self._writer.close()
        try:
            await self._writer.wait_closed()
        except ConnectionError:
            pass


def make_handler(
    gemini_client: genai.Client,
    model: str,
    store: BookingStore,
    mcp_client: McpClient | None,
    call_manager: CallManager | None = None,
    settings_store: Any = None,
    faq_store: Any = None,
    ivr_store: Any = None,
):
    async def handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        conn = AudioSocketConnection(reader, writer)
        logger.info("PBX connection opened: %s", conn.remote_address)
        session = CallSession(conn, gemini_client, model, store, mcp_client, call_manager, settings_store, faq_store,
                              ivr_store)
        try:
            await session.run()
        finally:
            await conn.close()
            logger.info("PBX connection closed: %s", conn.remote_address)

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
    faq_store: Any = None,
    ivr_store: Any = None,
):
    handler = make_handler(gemini_client, model, store, mcp_client, call_manager, settings_store, faq_store,
                           ivr_store)
    return await asyncio.start_server(handler, host, port)
