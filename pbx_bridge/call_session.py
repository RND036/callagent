"""Owns one phone call end-to-end: bridges Asterisk's AudioSocket TCP
connection (TLV-framed: a UUID packet, then PCM16 audio/DTMF/hangup packets)
to a Gemini Live session, running both directions concurrently until the
call ends or either side drops.
"""
from __future__ import annotations

import asyncio
import logging
import uuid
from typing import Any

from google import genai

from audio.codec import InboundTranscoder, OutboundTranscoder
from data.call_manager import CallManager
from data.store import BookingStore
from gemini_session.session import GeminiLiveSession
from pbx_bridge.audiosocket_protocol import (
    AudioSocketAdapter,
    ControlProtocolError,
    DtmfEvent,
    HangupEvent,
    StartEvent,
    UnknownEvent,
)
from pbx_bridge.mcp_client import McpClient
from tools.context import CallContext

import config

logger = logging.getLogger(__name__)

_OUTBOUND_FRAME_SECONDS = 0.02
_SAMPLE_WIDTH_BYTES = 2  # 16-bit PCM


class PacedMediaSender:
    """Sends outbound audio to Asterisk as 20ms PCM16 media packets, paced
    against a monotonic deadline (not sleep-after-send, which lets drift
    compound). There is no flush/clear event in the protocol -- barge_in()
    can only stop *further* queued chunks from going out; audio already sent
    will still play out on the Asterisk side."""

    def __init__(self, ws, control: AudioSocketAdapter, sample_rate: int, call_uuid: str | None = None) -> None:
        self._ws = ws
        self._control = control
        self._sample_rate = sample_rate
        self._call_uuid = call_uuid
        self._frame_bytes = int(sample_rate * _OUTBOUND_FRAME_SECONDS) * _SAMPLE_WIDTH_BYTES
        self._queue: asyncio.Queue[bytes] = asyncio.Queue()
        self._remainder = bytearray()
        self._task = asyncio.create_task(self._run())

    def enqueue(self, pcm_bytes: bytes) -> None:
        self._remainder.extend(pcm_bytes)
        while len(self._remainder) >= self._frame_bytes:
            frame = bytes(self._remainder[: self._frame_bytes])
            del self._remainder[: self._frame_bytes]
            self._queue.put_nowait(frame)

    def flush(self) -> None:
        """Pad any remaining partial frame with PCM16 silence (zero bytes) and send it."""
        if self._remainder:
            padding_needed = self._frame_bytes - len(self._remainder)
            frame = bytes(self._remainder) + bytes(padding_needed)
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
                await self._ws.send_packet(*self._control.build_audio_packet(frame, self._sample_rate))
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
        call_manager: CallManager | None = None,
        settings_store: Any = None,
    ) -> None:
        self._pbx_ws = pbx_ws
        self._gemini_client = gemini_client
        self._model = model
        self._store = store
        self._mcp_client = mcp_client
        self._call_manager = call_manager
        self._settings_store = settings_store
        self._control = AudioSocketAdapter()

    async def run(self) -> None:
        start = await self._wait_for_start()
        if start is None:
            return  # Asterisk socket closed before we ever got a usable start event

        if self._call_manager:
            await self._call_manager.call_started(start.call_sid, start.caller_id_number)

        ctx = CallContext(
            store=self._store,
            mcp=self._mcp_client,
            call_sid=start.call_sid,
            other_leg_call_id=start.other_leg_call_id,
            account_id=start.account_id,
            caller_number=start.caller_id_number,
            call_manager=self._call_manager,
            settings_store=self._settings_store,
        )
        sender = PacedMediaSender(self._pbx_ws, self._control, config.AUDIOSOCKET_SAMPLE_RATE_HZ, start.call_sid)
        try:
            async with GeminiLiveSession(self._gemini_client, self._model, ctx) as session:
                inbound = InboundTranscoder()
                outbound = OutboundTranscoder()

                pbx_to_gemini = asyncio.create_task(
                    self._pump_pbx_to_gemini(session, ctx, inbound, sender),
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
                    await self._send_transfer(ctx.ws_transfer_target)
        except Exception:  # noqa: BLE001 - log and end the call, don't crash the server
            logger.exception("Call %s ended with an error", start.call_sid)
        finally:
            await sender.close()
            if self._call_manager and start:
                status = "transferred" if ctx.ws_transfer_target else "completed"
                await self._call_manager.call_ended(start.call_sid, status=status)

    async def _wait_for_start(self) -> StartEvent | None:
        try:
            packet = await asyncio.wait_for(self._pbx_ws.recv_packet(), timeout=config.START_TIMEOUT_SECONDS)
        except asyncio.TimeoutError:
            logger.warning("No start UUID packet within %ss; proceeding with defaults", config.START_TIMEOUT_SECONDS)
            return self._default_start()

        if packet is None:
            logger.info("Asterisk socket closed before stream UUID arrived")
            return None

        msg_type, payload = packet
        try:
            event = self._control.parse_packet(msg_type, payload)
        except ControlProtocolError:
            logger.warning("Malformed start packet: type=%s payload=%r", msg_type, payload)
            return self._default_start()

        if not isinstance(event, StartEvent):
            logger.warning("Expected UUID packet first, got type=%s; proceeding with defaults", msg_type)
            return self._default_start()

        logger.info("Call started: %s", event.call_sid)
        return event

    def _default_start(self) -> StartEvent:
        return StartEvent(
            stream_sid=str(uuid.uuid4()),
            call_sid=None,
            other_leg_call_id=None,
            account_id=None,
            caller_id_number=None,
            sample_rate=config.AUDIOSOCKET_SAMPLE_RATE_HZ,
        )

    async def _pump_pbx_to_gemini(
        self,
        session: GeminiLiveSession,
        ctx: CallContext,
        inbound: InboundTranscoder,
        sender: PacedMediaSender,
    ) -> None:
        frame_count = 0
        while True:
            packet = await self._pbx_ws.recv_packet()
            if packet is None:
                return

            msg_type, payload = packet
            try:
                event = self._control.parse_packet(
                    msg_type, payload, expected_sample_rate=config.AUDIOSOCKET_SAMPLE_RATE_HZ
                )
            except ControlProtocolError:
                logger.warning("Ignoring malformed packet: type=%s payload=%r", msg_type, payload)
                continue

            if isinstance(event, HangupEvent):
                logger.info("Call ended by Asterisk (reason: %s)", event.reason)
                return
            elif isinstance(event, StartEvent):
                # UUID packet, already consumed in _wait_for_start(); ignore if it repeats.
                pass
            elif isinstance(event, DtmfEvent):
                await self._handle_dtmf(event.digit, session, sender)
            elif isinstance(event, UnknownEvent):
                logger.info("Unhandled Asterisk control packet: type=%s raw=%r", event.msg_type, event.raw)
            else:  # MediaEvent
                frame_count += 1
                if frame_count % 150 == 0:
                    logger.debug("Received %d audio packets from Asterisk for call %s", frame_count, ctx.call_sid)
                pcm16k = inbound.transcode(event.payload)
                session.send_audio_chunk_nowait(pcm16k)

            if ctx.hangup_requested:
                logger.info("Ending call: end_call tool invoked")
                return

    async def _handle_dtmf(self, digit: str, session: GeminiLiveSession, sender: PacedMediaSender) -> None:
        logger.info("DTMF digit: %s", digit)
        # Barge-in: the protocol has no flush/clear event, so the only lever
        # is to stop sending further queued chunks -- there's an unavoidable
        # tail from Asterisk's own playout buffer.
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

    async def _send_transfer(self, target: str) -> None:
        # AudioSocket has no transfer primitive either (unlike hangup --
        # build_hangup_packet -- there's no protocol-level equivalent of
        # dialplan Dial()/transfer a connected socket client can trigger);
        # transfer goes solely through MCP (see mcp_client.py). This is a
        # log-only fallback for when MCP transfer wasn't available.
        logger.warning("No AudioSocket-level transfer path; relying on MCP for transfer (target=%s)", target)
