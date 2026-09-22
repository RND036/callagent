from __future__ import annotations

import uuid as uuid_module
from unittest.mock import AsyncMock

import pytest

from pbx_bridge.audiosocket_protocol import AudioSocketAdapter, StartEvent
from pbx_bridge.call_session import CallSession, PacedMediaSender
from tools.context import CallContext


class FakePbxConn:
    """Fake AudioSocketConnection: recv_packet() pops the next queued
    (msg_type, payload) tuple, returning None once exhausted -- mimicking a
    connection that closes when the queue runs out."""

    def __init__(self, packets: list[tuple[int, bytes]]) -> None:
        self._packets = list(packets)

    async def recv_packet(self) -> tuple[int, bytes] | None:
        if not self._packets:
            return None
        return self._packets.pop(0)


class FakeGeminiSession:
    def __init__(self) -> None:
        self.audio_chunks: list[bytes] = []
        self.texts: list[str] = []

    def send_audio_chunk_nowait(self, pcm16_bytes: bytes) -> None:
        self.audio_chunks.append(pcm16_bytes)

    async def send_text(self, text: str) -> None:
        self.texts.append(text)


def make_session(pbx_conn) -> CallSession:
    return CallSession(pbx_conn, gemini_client=None, model="test-model", store=None, mcp_client=None)


def make_ctx(**overrides) -> CallContext:
    fields = dict(
        store=None, mcp=None, call_sid="call-1", other_leg_call_id=None, account_id=None, caller_number=None
    )
    fields.update(overrides)
    return CallContext(**fields)


def uuid_packet(call_uuid: uuid_module.UUID | None = None) -> tuple[int, bytes]:
    return 0x01, (call_uuid or uuid_module.uuid4()).bytes


def audio_packet(payload: bytes, sample_rate: int = 8000) -> tuple[int, bytes]:
    msg_type = {8000: 0x10, 16000: 0x12}[sample_rate]
    return msg_type, payload


def test_init_uses_audiosocket_adapter() -> None:
    session = make_session(FakePbxConn([]))
    assert isinstance(session._control, AudioSocketAdapter)


@pytest.mark.asyncio
async def test_wait_for_start_parses_uuid_packet() -> None:
    call_uuid = uuid_module.uuid4()
    session = make_session(FakePbxConn([uuid_packet(call_uuid)]))
    start = await session._wait_for_start()
    assert start == StartEvent(
        stream_sid=str(call_uuid),
        call_sid=str(call_uuid),
        other_leg_call_id=None,
        account_id=None,
        caller_id_number=None,
    )


@pytest.mark.asyncio
async def test_wait_for_start_malformed_uuid_falls_back_to_default() -> None:
    session = make_session(FakePbxConn([(0x01, b"too-short")]))
    start = await session._wait_for_start()
    assert start is not None
    assert start.call_sid is None


@pytest.mark.asyncio
async def test_wait_for_start_audio_first_falls_back_to_default() -> None:
    session = make_session(FakePbxConn([audio_packet(b"\x00\x01\x02\x03")]))
    start = await session._wait_for_start()
    assert start is not None
    assert start.call_sid is None


@pytest.mark.asyncio
async def test_wait_for_start_socket_closed_returns_none() -> None:
    session = make_session(FakePbxConn([]))
    start = await session._wait_for_start()
    assert start is None


@pytest.mark.asyncio
async def test_pump_pbx_to_gemini_forwards_audio() -> None:
    pcm_frame = b"\x00\x01" * 320
    pbx_conn = FakePbxConn([audio_packet(pcm_frame)])
    session = make_session(pbx_conn)
    gemini = FakeGeminiSession()
    ctx = make_ctx()
    inbound = _passthrough_inbound()
    sender = AsyncMock(spec=PacedMediaSender)

    await session._pump_pbx_to_gemini(gemini, ctx, inbound, sender)

    assert gemini.audio_chunks == [pcm_frame]


@pytest.mark.asyncio
async def test_pump_pbx_to_gemini_dtmf_triggers_barge_in_and_text() -> None:
    pbx_conn = FakePbxConn([(0x03, b"5")])
    session = make_session(pbx_conn)
    gemini = FakeGeminiSession()
    ctx = make_ctx()
    inbound = _passthrough_inbound()
    sender = AsyncMock(spec=PacedMediaSender)

    await session._pump_pbx_to_gemini(gemini, ctx, inbound, sender)

    sender.barge_in.assert_called_once()
    assert gemini.texts == ["[caller pressed keypad digit: 5]"]


@pytest.mark.asyncio
async def test_pump_pbx_to_gemini_stops_on_terminate_packet() -> None:
    pcm_frame = b"\x00\x01" * 320
    pbx_conn = FakePbxConn([(0x00, b""), audio_packet(pcm_frame)])
    session = make_session(pbx_conn)
    gemini = FakeGeminiSession()
    ctx = make_ctx()
    inbound = _passthrough_inbound()
    sender = AsyncMock(spec=PacedMediaSender)

    await session._pump_pbx_to_gemini(gemini, ctx, inbound, sender)

    # Loop should have returned on the terminate packet before consuming the trailing audio frame.
    assert gemini.audio_chunks == []


@pytest.mark.asyncio
async def test_pump_pbx_to_gemini_ignores_unknown_packets() -> None:
    pcm_frame = b"\x00\x01" * 320
    pbx_conn = FakePbxConn([(0xFF, b"\x01"), (0x42, b"???"), audio_packet(pcm_frame)])
    session = make_session(pbx_conn)
    gemini = FakeGeminiSession()
    ctx = make_ctx()
    inbound = _passthrough_inbound()
    sender = AsyncMock(spec=PacedMediaSender)

    await session._pump_pbx_to_gemini(gemini, ctx, inbound, sender)

    assert gemini.audio_chunks == [pcm_frame]


@pytest.mark.asyncio
async def test_pump_pbx_to_gemini_stops_when_hangup_requested() -> None:
    pcm_frame = b"\x00\x01" * 320
    pbx_conn = FakePbxConn([audio_packet(pcm_frame), audio_packet(pcm_frame)])
    session = make_session(pbx_conn)
    gemini = FakeGeminiSession()
    ctx = make_ctx(hangup_requested=True)
    inbound = _passthrough_inbound()
    sender = AsyncMock(spec=PacedMediaSender)

    await session._pump_pbx_to_gemini(gemini, ctx, inbound, sender)

    # Returns right after the first frame, never touching the second.
    assert gemini.audio_chunks == [pcm_frame]


@pytest.mark.asyncio
async def test_paced_media_sender_framing_and_flush_pcm16() -> None:
    mock_ws = AsyncMock()
    sample_rate = 8000
    frame_bytes = int(sample_rate * 0.02) * 2
    sender = PacedMediaSender(mock_ws, AudioSocketAdapter(), sample_rate, call_uuid="test-call")

    extra = 40
    sender.enqueue(b"\xaa" * (frame_bytes + extra))
    assert sender._queue.qsize() == 1
    frame1 = sender._queue.get_nowait()
    assert len(frame1) == frame_bytes
    assert len(sender._remainder) == extra

    sender.flush()
    assert sender._queue.qsize() == 1
    flushed_frame = sender._queue.get_nowait()
    assert len(flushed_frame) == frame_bytes
    assert flushed_frame == (b"\xaa" * extra) + (b"\x00" * (frame_bytes - extra))
    assert len(sender._remainder) == 0

    sender.enqueue(b"\xbb" * 100)
    sender.barge_in()
    assert len(sender._remainder) == 0
    assert sender._queue.qsize() == 0

    await sender.close()


class _IdentityInbound:
    """Stand-in for InboundTranscoder that doesn't resample -- these tests
    exercise the pump loop's packet dispatch, not codec correctness (see
    test_codec.py for real resampling behavior)."""

    def transcode(self, pcm_chunk: bytes) -> bytes:
        return pcm_chunk


def _passthrough_inbound():
    return _IdentityInbound()
