"""Transport-agnostic line interface, so the agent (gemini_session/live_session.py) never touches a socket.

Audio contract with the AI:
  - receive(): caller audio as PCM16 little-endian, mono, 16 kHz (None when the caller hung up)
  - send():    agent audio as PCM16 little-endian, mono, 24 kHz
Implementations: AsteriskTransport (AudioSocket, 8 kHz on the wire) and the dashboard's BrowserTransport.
Keypresses: while receive() runs, the transport puts every pressed key into its `dtmf` queue.
"""
from __future__ import annotations

import asyncio
from typing import Protocol

INPUT_RATE = 16000
OUTPUT_RATE = 24000


class AudioTransport(Protocol):
    dtmf: asyncio.Queue[str]

    async def receive(self) -> bytes | None: ...

    async def send(self, pcm24k: bytes) -> None: ...

    async def clear(self) -> None:
        """Drop queued agent audio (the caller interrupted)."""

    async def event(self, data: dict) -> None:
        """Optional UI side-channel. May be a no-op."""

    async def close(self) -> None:
        """Let queued audio finish, then hang up."""
