"""Owns one phone call end-to-end: bridges the SmartPBX Dialog WebSocket
(JSON start/media/dtmf/hangup events) to a Gemini Live session, running both
directions concurrently until the call ends or either side drops.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid

from google import genai

from audio.codec import InboundTranscoder, OutboundTranscoder
from data.store import BookingStore
from gemini_session.session import GeminiLiveSession
from pbx_bridge.control_protocol import (
    ControlProtocolError,
    DtmfEvent,
    HangupEvent,
    MediaEvent,
    PBXControlAdapter,
    StartEvent,
    UnknownEvent,
)
from pbx_bridge.inband_dtmf import InbandDtmfDetector
from pbx_bridge.mcp_client import McpClient
from tools.context import CallContext

import config

logger = logging.getLogger(__name__)

_OUTBOUND_FRAME_BYTES = 160  # 20ms of 8kHz mu-law
_OUTBOUND_FRAME_SECONDS = 0.02


class DtmfDedupe:
    """SmartPBX's dtmf event and our in-band tone detector can both report
    the same keypress; only the first one within the window counts."""

    def __init__(self, window_seconds: float) -> None:
        self._window = window_seconds
        self._last_digit: str | None = None
        self._last_seen: float = 0.0

    def accept(self, digit: str) -> bool:
        now = time.monotonic()
        if self._last_digit == digit and (now - self._last_seen) < self._window:
            self._last_seen = now
            return False
        self._last_digit = digit
        self._last_seen = now
        return True


class PacedMediaSender:
    """Sends outbound audio to the PBX as 20ms media frames, paced against a
    monotonic deadline (not sleep-after-send, which lets drift compound).
    There is no flush/clear event in the protocol -- barge_in() can only stop
    *further* queued chunks from going out; audio already sent will still
    play out on the PBX side."""

    def __init__(self, ws, control: PBXControlAdapter, stream_sid: str) -> None:
        self._ws = ws
        self._control = control
        self._stream_sid = stream_sid
        self._queue: asyncio.Queue[bytes] = asyncio.Queue()
        self._remainder = bytearray()
        self._task = asyncio.create_task(self._run())

    def enqueue(self, ulaw_bytes: bytes) -> None:
        self._remainder.extend(ulaw_bytes)
        while len(self._remainder) >= _OUTBOUND_FRAME_BYTES:
            frame = bytes(self._remainder[:_OUTBOUND_FRAME_BYTES])
            del self._remainder[:_OUTBOUND_FRAME_BYTES]
            self._queue.put_nowait(frame)

    def flush(self) -> None:
        """Pad any remaining partial frame with mu-law silence (0xFF) and send it."""
        if self._remainder:
            padding_needed = _OUTBOUND_FRAME_BYTES - len(self._remainder)
            frame = bytes(self._remainder) + (b"\xff" * padding_needed)
            self._remainder.clear()
            self._queue.put_nowait(frame)

    def barge_in(self) -> None:
        self._remainder.clear()
        while True:
            try:
                self._queue.get_nowait()
            except asyncio.QueueEmpty:
                return

    async def close(self) -> None:
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass

    async def _run(self) -> None:
        loop = asyncio.get_event_loop()
        next_deadline = loop.time()
        while True:
            frame = await self._queue.get()
            now = loop.time()
            next_deadline = max(next_deadline + _OUTBOUND_FRAME_SECONDS, now)
            await asyncio.sleep(next_deadline - now)
            try:
                await self._ws.send(json.dumps(self._control.build_media_frame(self._stream_sid, frame)))
            except Exception:  # noqa: BLE001 - socket may already be closed; let the reader loop notice
                return


class CallSession:
    def __init__(
        self,
        pbx_ws,
        gemini_client: genai.Client,
        model: str,
        store: BookingStore,
        mcp_client: McpClient | None = None,
    ) -> None:
        self._pbx_ws = pbx_ws
        self._gemini_client = gemini_client
        self._model = model
        self._store = store
        self._mcp_client = mcp_client
        self._control = PBXControlAdapter()

    async def run(self) -> None:
        start = await self._wait_for_start()
        if start is None:
            return  # PBX socket closed before we ever got a usable start event
        ctx = CallContext(
            store=self._store,
            mcp=self._mcp_client,
            call_sid=start.call_sid,
            other_leg_call_id=start.other_leg_call_id,
            account_id=start.account_id,
            caller_number=start.caller_id_number,
        )
        sender = PacedMediaSender(self._pbx_ws, self._control, start.stream_sid)
        try:
            async with GeminiLiveSession(self._gemini_client, self._model, ctx) as session:
                inbound = InboundTranscoder()
                outbound = OutboundTranscoder()
                inband_dtmf = InbandDtmfDetector()
                dedupe = DtmfDedupe(config.DTMF_DEDUPE_WINDOW_SECONDS)

                pbx_to_gemini = asyncio.create_task(
                    self._pump_pbx_to_gemini(session, ctx, inbound, inband_dtmf, dedupe, sender),
                    name="pbx-to-gemini",
                )
                gemini_to_pbx = asyncio.create_task(
                    self._pump_gemini_to_pbx(session, outbound, sender),
                    name="gemini-to-pbx",
                )

                # Prompt the agent to speak first with the trilingual greeting
                await session.trigger_greeting()

                done, pending = await asyncio.wait(
                    {pbx_to_gemini, gemini_to_pbx}, return_when=asyncio.FIRST_COMPLETED
                )
                for task in done:
                    exc = task.exception()
                    if exc:
                        logger.error("Task %s raised error: %s", task.get_name(), exc, exc_info=exc)
                    else:
                        logger.info("Task %s completed normally", task.get_name())
                for task in pending:
                    task.cancel()
                for task in done | pending:
                    try:
                        await task
                    except asyncio.CancelledError:
                        pass

                if ctx.ws_transfer_target:
                    await self._send_transfer(start.stream_sid, ctx.ws_transfer_target)
        except Exception:  # noqa: BLE001 - log and end the call, don't crash the server
            logger.exception("Call %s ended with an error", start.call_sid)
        finally:
            await sender.close()

    async def _wait_for_start(self) -> StartEvent | None:
        try:
            message = await asyncio.wait_for(self._pbx_ws.recv(), timeout=config.START_TIMEOUT_SECONDS)
        except asyncio.TimeoutError:
            logger.warning("No start event within %ss; proceeding with defaults", config.START_TIMEOUT_SECONDS)
            return self._default_start()
        except Exception:  # noqa: BLE001 - socket closed before anything arrived
            logger.info("PBX socket closed before a start event arrived")
            return None

        try:
            event = self._control.parse(json.loads(message))
        except (json.JSONDecodeError, ControlProtocolError):
            logger.warning("Malformed first frame, expected start event: %r", message)
            return self._default_start()

        if not isinstance(event, StartEvent):
            logger.warning("Expected start event first, got %s; proceeding with defaults", type(event).__name__)
            return self._default_start()

        logger.info("Call started: %s (stream %s, from %s)", event.call_sid, event.stream_sid, event.caller_id_number)
        return event

    def _default_start(self) -> StartEvent:
        return StartEvent(
            stream_sid=str(uuid.uuid4()),
            call_sid=None,
            other_leg_call_id=None,
            account_id=None,
            caller_id_number=None,
        )

    async def _pump_pbx_to_gemini(
        self,
        session: GeminiLiveSession,
        ctx: CallContext,
        inbound: InboundTranscoder,
        inband_dtmf: InbandDtmfDetector,
        dedupe: DtmfDedupe,
        sender: PacedMediaSender,
    ) -> None:
        frame_count = 0
        async for message in self._pbx_ws:
            try:
                event = self._control.parse(json.loads(message))
            except (json.JSONDecodeError, ControlProtocolError):
                logger.warning("Ignoring malformed frame: %r", message)
                continue

            if isinstance(event, MediaEvent):
                frame_count += 1
                if frame_count % 150 == 0:
                    logger.debug("Received %d audio frames from PBX for call %s", frame_count, ctx.call_sid)
                pcm16k = inbound.transcode(event.payload)
                session.send_audio_chunk_nowait(pcm16k)

                digit = inband_dtmf.feed_ulaw(event.payload)
                if digit is not None and dedupe.accept(digit):
                    await self._handle_dtmf(digit, session, sender)
            elif isinstance(event, DtmfEvent):
                if dedupe.accept(event.digit):
                    await self._handle_dtmf(event.digit, session, sender)
            elif isinstance(event, HangupEvent):
                logger.info("Call ended by PBX (reason: %s)", event.reason)
                return
            elif isinstance(event, UnknownEvent):
                logger.info("Unhandled PBX event: %r", event.raw)

            if ctx.hangup_requested:
                logger.info("Ending call: end_call tool invoked")
                return

    async def _handle_dtmf(self, digit: str, session: GeminiLiveSession, sender: PacedMediaSender) -> None:
        logger.info("DTMF digit: %s", digit)
        # Barge-in: the protocol has no flush/clear event, so the only lever
        # is to stop sending further queued chunks -- there's an unavoidable
        # tail from the PBX's own playout buffer.
        sender.barge_in()
        await session.send_text(f"[caller pressed keypad digit: {digit}]")

    async def _pump_gemini_to_pbx(
        self, session: GeminiLiveSession, outbound: OutboundTranscoder, sender: PacedMediaSender
    ) -> None:
        async for event in session.events():
            if event.interrupted:
                logger.info("Barge-in: agent speech interrupted by caller")
                sender.barge_in()
            elif event.audio:
                sender.enqueue(outbound.transcode(event.audio))
            if event.turn_complete:
                sender.flush()

    async def _send_transfer(self, stream_sid: str, target: str) -> None:
        try:
            await self._pbx_ws.send(json.dumps(self._control.build_transfer_frame(stream_sid, target)))
        except Exception:  # noqa: BLE001 - socket may already be closed
            logger.debug("Could not send WS-level transfer frame; socket likely closed")
