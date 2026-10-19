from __future__ import annotations

import asyncio

import pytest

from pbx_bridge.asterisk_transport import AsteriskTransport


class SlowConn:
    def __init__(self) -> None:
        self.queue: asyncio.Queue = asyncio.Queue()
        self.reads = 0
        self.sent = []

    async def recv_packet(self):
        self.reads += 1
        return await self.queue.get()

    async def send_packet(self, msg_type, payload):
        self.sent.append((msg_type, payload))


@pytest.mark.asyncio
async def test_cancelled_receive_does_not_lose_the_next_packet() -> None:
    conn = SlowConn()
    transport = AsteriskTransport(conn)
    first = asyncio.create_task(transport.receive())
    await asyncio.sleep(0.05)
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    await conn.queue.put((0x10, b"\x01\x00" * 160))
    audio = await asyncio.wait_for(transport.receive(), timeout=2)
    assert audio  # the packet that arrived while nobody was listening is still delivered
    assert conn.reads == 1  # the same in-flight read was reused, not a second one racing it
    await transport.close()


@pytest.mark.asyncio
async def test_play_frames_line_rate_audio() -> None:
    conn = SlowConn()
    transport = AsteriskTransport(conn)
    await transport.play(b"\x01\x00" * 250)  # 1.5 frames of 160 samples
    await asyncio.wait_for(transport.wait_played(), timeout=2)
    audio = [p for t, p in conn.sent if t == 0x10 and any(p)]
    assert len(audio) == 2 and all(len(p) == 320 for p in audio)
    await transport.close()
