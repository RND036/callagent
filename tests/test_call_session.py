from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

import config
from pbx_bridge.audio_stream_protocol import FsAudioStreamAdapter, StartEvent
from pbx_bridge.call_session import CallSession, PacedMediaSender
from tools.context import CallContext


class FakePbxWs:
    """Fake FreeSWITCH WS connection: `recv()` pops the next queued message
    (for _wait_for_start), and async-iterating pops the rest (for
    _pump_pbx_to_gemini), mimicking a connection that closes once the queue
    is exhausted."""

    def __init__(self, messages: list) -> None:
        self._messages = list(messages)

    async def recv(self):
        if not self._messages:
            raise ConnectionResetError("closed")
        return self._messages.pop(0)

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self._messages:
            raise StopAsyncIteration
        return self._messages.pop(0)


class FakeGeminiSession:
    def __init__(self) -> None:
        self.audio_chunks: list[bytes] = []
        self.texts: list[str] = []

    def send_audio_chunk_nowait(self, pcm16_bytes: bytes) -> None:
        self.audio_chunks.append(pcm16_bytes)

    async def send_text(self, text: str) -> None:
        self.texts.append(text)


class FakeInbandDtmf:
    """Returns a fixed digit on the first frame fed, None afterwards."""

    def __init__(self, digit: str | None) -> None:
        self._digit = digit
        self._fired = False

    def feed_pcm16(self, pcm16_chunk: bytes) -> str | None:
        if self._digit is not None and not self._fired:
            self._fired = True
            return self._digit
        return None


def make_session(pbx_ws) -> CallSession:
    return CallSession(pbx_ws, gemini_client=None, model="test-model", store=None, mcp_client=None)


def make_ctx(**overrides) -> CallContext:
    fields = dict(
        store=None, mcp=None, call_sid="call-1", other_leg_call_id=None, account_id=None, caller_number=None
    )
    fields.update(overrides)
    return CallContext(**fields)


def test_init_uses_fs_audio_stream_adapter() -> None:
    session = make_session(FakePbxWs([]))
    assert isinstance(session._control, FsAudioStreamAdapter)


@pytest.mark.asyncio
async def test_wait_for_start_parses_metadata() -> None:
    metadata = "call_uuid=abc-123;caller_id_number=+94771234567;destination_number=1001;sample_rate=16000"
    session = make_session(FakePbxWs([metadata]))
    start = await session._wait_for_start()
    assert start == StartEvent(
        stream_sid="abc-123",
        call_sid="abc-123",
        other_leg_call_id=None,
        account_id="1001",
        caller_id_number="+94771234567",
        sample_rate=16000,
    )


@pytest.mark.asyncio
async def test_wait_for_start_malformed_metadata_falls_back_to_default() -> None:
    session = make_session(FakePbxWs(["not valid metadata"]))
    start = await session._wait_for_start()
    assert start is not None
    assert start.call_sid is None


@pytest.mark.asyncio
async def test_wait_for_start_binary_first_falls_back_to_default() -> None:
    session = make_session(FakePbxWs([b"\x00\x01\x02\x03"]))
    start = await session._wait_for_start()
    assert start is not None
    assert start.call_sid is None


@pytest.mark.asyncio
async def test_wait_for_start_socket_closed_returns_none() -> None:
    session = make_session(FakePbxWs([]))
    start = await session._wait_for_start()
    assert start is None


@pytest.mark.asyncio
async def test_pump_pbx_to_gemini_forwards_binary_audio() -> None:
    pcm_frame = b"\x00\x01" * 320  # 640 bytes of fake PCM16
    pbx_ws = FakePbxWs([pcm_frame])
    session = make_session(pbx_ws)
    gemini = FakeGeminiSession()
    ctx = make_ctx()
    inbound = _passthrough_inbound()
    sender = AsyncMock(spec=PacedMediaSender)

    await session._pump_pbx_to_gemini(gemini, ctx, inbound, FakeInbandDtmf(None), sender)

    assert gemini.audio_chunks == [pcm_frame]  # passthrough since rates match by default


@pytest.mark.asyncio
async def test_pump_pbx_to_gemini_dtmf_triggers_barge_in_and_text() -> None:
    pcm_frame = b"\x00\x01" * 320
    pbx_ws = FakePbxWs([pcm_frame])
    session = make_session(pbx_ws)
    gemini = FakeGeminiSession()
    ctx = make_ctx()
    inbound = _passthrough_inbound()
    sender = AsyncMock(spec=PacedMediaSender)

    await session._pump_pbx_to_gemini(gemini, ctx, inbound, FakeInbandDtmf("5"), sender)

    sender.barge_in.assert_called_once()
    assert gemini.texts == ["[caller pressed keypad digit: 5]"]


@pytest.mark.asyncio
async def test_pump_pbx_to_gemini_stops_on_hangup_control_message() -> None:
    pbx_ws = FakePbxWs(['{"type": "mod_audio_stream::disconnect"}', b"\x00\x01" * 320])
    session = make_session(pbx_ws)
    gemini = FakeGeminiSession()
    ctx = make_ctx()
    inbound = _passthrough_inbound()
    sender = AsyncMock(spec=PacedMediaSender)

    await session._pump_pbx_to_gemini(gemini, ctx, inbound, FakeInbandDtmf(None), sender)

    # Loop should have returned on the hangup message before consuming the trailing audio frame.
    assert gemini.audio_chunks == []


@pytest.mark.asyncio
async def test_pump_pbx_to_gemini_ignores_malformed_and_unknown_control_messages() -> None:
    pcm_frame = b"\x00\x01" * 320
    pbx_ws = FakePbxWs(["not json", '{"type": "mod_audio_stream::json"}', pcm_frame])
    session = make_session(pbx_ws)
    gemini = FakeGeminiSession()
    ctx = make_ctx()
    inbound = _passthrough_inbound()
    sender = AsyncMock(spec=PacedMediaSender)

    await session._pump_pbx_to_gemini(gemini, ctx, inbound, FakeInbandDtmf(None), sender)

    assert gemini.audio_chunks == [pcm_frame]


@pytest.mark.asyncio
async def test_pump_pbx_to_gemini_stops_when_hangup_requested() -> None:
    pcm_frame = b"\x00\x01" * 320
    pbx_ws = FakePbxWs([pcm_frame, pcm_frame])
    session = make_session(pbx_ws)
    gemini = FakeGeminiSession()
    ctx = make_ctx(hangup_requested=True)
    inbound = _passthrough_inbound()
    sender = AsyncMock(spec=PacedMediaSender)

    await session._pump_pbx_to_gemini(gemini, ctx, inbound, FakeInbandDtmf(None), sender)

    # Returns right after the first frame, never touching the second.
    assert gemini.audio_chunks == [pcm_frame]


@pytest.mark.asyncio
async def test_paced_media_sender_framing_and_flush_pcm16() -> None:
    mock_ws = AsyncMock()
    sample_rate = config.FS_STREAM_SAMPLE_RATE_HZ
    frame_bytes = int(sample_rate * 0.02) * 2
    sender = PacedMediaSender(mock_ws, FsAudioStreamAdapter(), sample_rate, call_uuid="test-call")

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


def _passthrough_inbound():
    from audio.codec import InboundTranscoder

    return InboundTranscoder()
