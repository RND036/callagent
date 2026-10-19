"""Asterisk AudioSocket line for one call: the stream-keeping half of the call.

What keeps a call from dropping here (ported from cmc-new's AsteriskTransport):
  - AudioSocket hangs up after ~2 s with no frame in either direction, and a trunk that suppresses silence sends
    nothing while the caller is quiet. Whenever there is nothing to play we send a 20 ms silent frame every 0.5 s.
  - A few frames are held back at the start of each talkspurt so network jitter doesn't leave gaps mid-word.
  - close() lets the goodbye play out before the terminate packet.
"""
from __future__ import annotations

import asyncio
import logging

import config
from audio.codec import InboundTranscoder, OutboundTranscoder
from audio.noise_gate import NearFieldGate
from audio.recorder import CallRecorder
from pbx_bridge.audiosocket_protocol import (
    AudioSocketAdapter,
    ControlProtocolError,
    DtmfEvent,
    HangupEvent,
    MediaEvent,
    UnknownEvent,
)

logger = logging.getLogger(__name__)

FRAME_SECONDS = 0.02
PREBUFFER_SECONDS = 0.06
KEEPALIVE_SECONDS = 0.5
GOODBYE_DRAIN_SECONDS = 10.0
_SAMPLE_WIDTH_BYTES = 2


class AsteriskTransport:
    def __init__(
        self,
        conn,
        control: AudioSocketAdapter | None = None,
        rate: int | None = None,
        recorder: CallRecorder | None = None,
        gate: NearFieldGate | None = None,
    ) -> None:
        self._conn = conn
        self._control = control or AudioSocketAdapter()
        self._rate = rate or config.AUDIOSOCKET_SAMPLE_RATE_HZ
        self._recorder = recorder
        self._gate = gate
        self._in = InboundTranscoder()
        self._out = OutboundTranscoder()
        self._frame_bytes = int(self._rate * FRAME_SECONDS) * _SAMPLE_WIDTH_BYTES
        self._buf = bytearray()
        self._queue: asyncio.Queue[bytes] = asyncio.Queue()
        self.dtmf: asyncio.Queue[str] = asyncio.Queue()
        self.closed = False
        # The packet read in flight. It outlives a cancelled receive() so the next call picks it up instead of
        # losing half a TLV packet (the language menu stops its listener before the AI starts listening).
        self._rx: asyncio.Future | None = None
        self._sender = asyncio.create_task(self._send_loop(), name="audiosocket-sender")

    @property
    def gate_open(self) -> bool:
        """The caller's voice is on the line right now (the near-field gate is open)."""
        return bool(self._gate and self._gate.is_open)

    def gate_summary(self) -> str | None:
        return self._gate.summary() if self._gate else None

    async def receive(self) -> bytes | None:
        while not self.closed:
            if self._rx is None:
                self._rx = asyncio.ensure_future(self._conn.recv_packet())
            packet = await asyncio.shield(self._rx)
            self._rx = None
            if packet is None:
                self.closed = True
                return None
            msg_type, payload = packet
            try:
                event = self._control.parse_packet(msg_type, payload, expected_sample_rate=self._rate)
            except ControlProtocolError:
                logger.warning("Ignoring malformed packet: type=%s payload=%r", msg_type, payload)
                continue
            if isinstance(event, HangupEvent):
                logger.info("Call ended by Asterisk (reason: %s)", event.reason)
                self.closed = True
                return None
            if isinstance(event, DtmfEvent):
                logger.info("DTMF digit: %s", event.digit)
                self.dtmf.put_nowait(event.digit)
            elif isinstance(event, MediaEvent):
                if self._recorder is not None:
                    self._recorder.add_caller(event.payload)  # raw line audio, before the gate
                # Gate at the native rate, before resampling: gated audio becomes silence.
                chunks = self._gate.process(event.payload) if self._gate else [event.payload]
                if chunks:
                    return self._in.transcode(b"".join(chunks))
            elif isinstance(event, UnknownEvent):
                logger.info("Unhandled Asterisk control packet: type=%s raw=%r", event.msg_type, event.raw)
        return None

    async def send(self, pcm24k: bytes) -> None:
        self._buf.extend(self._out.transcode(pcm24k))
        while len(self._buf) >= self._frame_bytes:
            self._queue.put_nowait(bytes(self._buf[: self._frame_bytes]))
            del self._buf[: self._frame_bytes]

    async def play(self, pcm: bytes) -> None:
        """Queue audio that is already at the line rate (a recorded prompt); `send()` is for the AI's 24 kHz."""
        self._buf.extend(pcm)
        while len(self._buf) >= self._frame_bytes:
            self._queue.put_nowait(bytes(self._buf[: self._frame_bytes]))
            del self._buf[: self._frame_bytes]
        if self._buf:  # the last, partial frame
            self._queue.put_nowait(bytes(self._buf) + bytes(self._frame_bytes - len(self._buf)))
            self._buf.clear()

    async def wait_played(self) -> None:
        """Until everything queued has been sent (or cleared)."""
        await self._queue.join()

    async def clear(self) -> None:
        self._buf.clear()
        while not self._queue.empty():
            self._queue.get_nowait()
            self._queue.task_done()

    async def event(self, data: dict) -> None:
        pass  # no UI side-channel on a phone call

    async def _write(self, frame: bytes) -> None:
        await self._conn.send_packet(*self._control.build_audio_packet(frame, self._rate))

    async def _send_loop(self) -> None:
        loop = asyncio.get_running_loop()
        deadline = loop.time()
        keepalive = bytes(self._frame_bytes)
        while True:
            try:
                if self._queue.empty():
                    try:
                        frame = await asyncio.wait_for(self._queue.get(), KEEPALIVE_SECONDS)
                    except asyncio.TimeoutError:
                        await self._write(keepalive)
                        continue
                    # Start of a talkspurt: let a few frames build up so the pacing below doesn't run dry.
                    await asyncio.sleep(PREBUFFER_SECONDS)
                    deadline = loop.time()
                else:
                    frame = self._queue.get_nowait()
                deadline = max(deadline + FRAME_SECONDS, loop.time())
                await asyncio.sleep(max(0.0, deadline - loop.time()))
                await self._write(frame)
                if self._recorder is not None:
                    self._recorder.add_agent(frame)  # only what was really sent
                self._queue.task_done()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - socket gone; the reader notices
                self.closed = True
                return

    async def _play_out(self) -> None:
        """Let everything queued reach the caller."""
        if self._buf:
            self._queue.put_nowait(bytes(self._buf) + bytes(self._frame_bytes - len(self._buf)))
            self._buf.clear()
        for _ in range(int(GOODBYE_DRAIN_SECONDS / 0.1)):
            if self.closed or self._queue.empty():
                break
            await asyncio.sleep(0.1)
        await asyncio.sleep(2 * FRAME_SECONDS + PREBUFFER_SECONDS)  # the last frame may still be in flight

    async def close(self) -> None:
        """Play out the goodbye, then send the AudioSocket terminate packet."""
        if self._rx is not None:
            self._rx.cancel()
            self._rx = None
        if self.closed:
            self._sender.cancel()
            return
        await self._play_out()
        self._sender.cancel()
        try:
            await self._sender
        except asyncio.CancelledError:
            pass
        if not self.closed:
            self.closed = True
            logger.info("Hanging up: sending AudioSocket terminate packet")
            try:
                await self._conn.send_packet(*self._control.build_hangup_packet())
            except Exception:  # noqa: BLE001
                logger.debug("Hangup packet not sent; socket already closed")
