"""Owns one phone call end-to-end: bridges Asterisk's AudioSocket TCP
connection (TLV-framed: a UUID packet, then PCM16 audio/DTMF/hangup packets)
to a Gemini Live session, running both directions concurrently until the
call ends or either side drops. Whatever happens, the call's transcript,
customer details and recording end up in Call History; if the AI could not
carry on, the staff also get an urgent callback message.
"""
from __future__ import annotations

import asyncio
import logging
import uuid
from typing import Any

from google import genai

from audio.noise_gate import build_caller_gate
from audio.recorder import CallRecorder
from data.call_manager import CallManager, TranscriptLog
from data.store import BookingStore
from gemini_session.persona import DEFAULT_LANGUAGE, LANGUAGE_MENU_KEY, LANGUAGES, normalize_language
from gemini_session.live_session import AgentResult, run_agent
from pbx_bridge.asterisk_transport import AsteriskTransport
from pbx_bridge.audiosocket_protocol import AudioSocketAdapter, ControlProtocolError, StartEvent
from pbx_bridge.ivr import run_language_menu
from pbx_bridge.mcp_client import McpClient
from tools.context import CallContext
from tools.handlers import _NOT_A_REAL_NUMBER

import config

logger = logging.getLogger(__name__)


async def request_callback(store, call_manager: CallManager | None, caller_number: str | None,
                           customer: dict[str, str], why: str) -> None:
    """The AI could not finish the call: leave the staff an urgent callback message with what is known.
    Nothing to do without a number to call back."""
    phone = str(customer.get("phone") or caller_number or "").strip()
    if store is None or phone.lower() in _NOT_A_REAL_NUMBER:
        return
    known = ", ".join(f"{key.replace('_', ' ')}: {value}" for key, value in customer.items()
                      if key not in ("name", "phone"))
    message = f"{why} Please call them back." + (f" So far: {known}." if known else "")
    try:
        await store.take_message(caller_name=customer.get("name"), caller_phone=phone, message=message,
                                 urgent=True, category="callback")
    except Exception:  # noqa: BLE001
        logger.exception("Failed to leave a callback message for %s", phone)
        return
    if call_manager is not None:
        await call_manager.broadcast("message_taken", {"caller_name": customer.get("name"), "caller_phone": phone,
                                                       "message": message, "urgent": True, "category": "callback"})


def call_status(ctx: CallContext, result: AgentResult | None) -> str:
    if ctx.ws_transfer_target:
        return "transferred"
    return "dropped" if result is None or result.reason == "error" else "completed"


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
        faq_store: Any = None,
        ivr_store: Any = None,
    ) -> None:
        self._pbx_ws = pbx_ws
        self._gemini_client = gemini_client
        self._model = model
        self._store = store
        self._mcp_client = mcp_client
        self._call_manager = call_manager
        self._settings_store = settings_store
        self._faq_store = faq_store
        self._ivr_store = ivr_store
        self._control = AudioSocketAdapter()

    async def run(self) -> None:
        start = await self._wait_for_start()
        if start is None:
            return  # Asterisk socket closed before we ever got a usable start event

        caller_number = start.caller_id_number
        language = DEFAULT_LANGUAGE
        dialplan_language: str | None = None  # picked in the dialplan's own menu (old setup): no menu of ours
        if self._call_manager:
            # AudioSocket carries no caller ID or IVR choice; the dialplan
            # registered both by UUID.
            pending = self._call_manager.take_pending_call(start.call_sid)
            if pending:
                caller_number = caller_number or pending.caller_number
                dialplan_language = pending.language
                language = pending.language or language
            await self._call_manager.call_started(start.call_sid, caller_number)
        logger.info("Call %s: caller=%s language=%s", start.call_sid, caller_number, language)

        ctx = CallContext(
            store=self._store,
            mcp=self._mcp_client,
            call_sid=start.call_sid,
            other_leg_call_id=start.other_leg_call_id,
            account_id=start.account_id,
            caller_number=caller_number,
            call_manager=self._call_manager,
            settings_store=self._settings_store,
            faq_store=self._faq_store,
            language=language,
        )
        recorder = (
            # Spooled to disk as the call goes on: a crash still leaves the audio (CallManager.recover_interrupted).
            CallRecorder(config.AUDIOSOCKET_SAMPLE_RATE_HZ, spool=config.RECORDINGS_DIR / start.call_sid)
            if config.RECORDINGS_ENABLED and start.call_sid
            else None
        )
        transcript = TranscriptLog(self._call_manager, start.call_sid)
        noise_gate = build_caller_gate()
        transport = AsteriskTransport(self._pbx_ws, self._control, recorder=recorder, gate=noise_gate)
        result: AgentResult | None = None
        try:
            if dialplan_language is None:
                if not await self._language_menu(transport, ctx):  # the caller hung up during the menu
                    result = AgentResult(reason="caller_hung_up_in_menu")
            if result is None:
                if self._ivr_store is not None and self._ivr_store.is_active():
                    ctx.language_menu_key = LANGUAGE_MENU_KEY  # the caller may press it to change language
                result = await self._run_agent_loop(transport, ctx, transcript)
            logger.info("Call %s ended: %s (Live reconnects: %d)", start.call_sid, result.reason, result.reconnects)
            if noise_gate is not None:
                logger.info("Noise gate for call %s: %s", start.call_sid, noise_gate.summary())
        except Exception:  # noqa: BLE001 - log and end the call, don't crash the server
            logger.exception("Call %s ended with an error", start.call_sid)
        finally:
            # Each step on its own: one failing must never cost the call its record or recording.
            try:
                await transport.close()  # plays out the goodbye, then sends the terminate packet
            except Exception:  # noqa: BLE001
                logger.exception("Call %s: closing the line failed", start.call_sid)
            try:
                await transcript.flush()
            except Exception:  # noqa: BLE001
                logger.exception("Call %s: saving the last transcript line failed", start.call_sid)
            recording_file = await self._save_recording(recorder, start.call_sid)
            status = call_status(ctx, result)
            if self._call_manager:
                await self._call_manager.call_ended(
                    start.call_sid, status=status, recording_file=recording_file,
                    end_reason=result.reason if result else "error",
                    error=result.error if result else "the agent crashed",
                    reconnects=result.reconnects if result else 0, customer=ctx.customer,
                )
            if status == "dropped":
                await request_callback(self._store, self._call_manager, caller_number, ctx.customer,
                                       "The AI receptionist lost its connection during this caller's call.")

    async def _run_agent_loop(self, transport: AsteriskTransport, ctx: CallContext, transcript) -> AgentResult:
        """Runs the agent; each time the caller presses the language-menu key it is stopped, the menu is played and
        a new agent carries on in the chosen language. The whole call keeps one time limit."""
        loop = asyncio.get_running_loop()
        started = loop.time()
        reconnects = 0
        changed = False
        while True:
            result = await run_agent(
                transport, ctx, transcript, self._gemini_client, self._model,
                max_seconds=max(1.0, config.MAX_CALL_SECONDS - (loop.time() - started)), language_changed=changed,
            )
            reconnects += result.reconnects
            if result.reason != "language_menu":
                break
            await transcript.flush()
            if not await self._language_menu(transport, ctx, mid_call=True):
                result = AgentResult(reason="caller_hung_up_in_menu")
                break
            await transcript.note("tool", f"the caller pressed {ctx.language_menu_key} and the language is now "
                                          f"{LANGUAGES[normalize_language(ctx.language)]}")
            changed = True
        result.reconnects = reconnects
        return result

    async def _language_menu(self, transport: AsteriskTransport, ctx: CallContext, mid_call: bool = False) -> bool:
        """Plays the language menu if it is switched on and sets ctx.language; with it off the menu's default
        language is used. Mid-call (the caller pressed the menu key) a menu nobody answers keeps the current
        language. Returns False when the caller hung up during the menu."""
        store = self._ivr_store
        if store is None:
            return True
        cfg = store.settings()
        if not store.is_active():
            ctx.language = cfg["default_language"] if cfg["enabled"] else ctx.language
            return True
        menu_pcm = store.load_prompt_pcm("menu")
        if menu_pcm is None:
            ctx.language = cfg["default_language"]
            return True
        choice = await run_language_menu(transport, cfg, menu_pcm, store.load_prompt_pcm("invalid"))
        if choice is None:
            return False
        previous = ctx.language
        if not (mid_call and choice.timed_out):
            ctx.language = choice.language
        while not transport.dtmf.empty():  # the menu key must not reach the model as a keypad note
            transport.dtmf.get_nowait()
        if self._call_manager and ctx.call_sid:
            if mid_call:
                await self._call_manager.record_action(ctx.call_sid, "Language Changed", {
                    "from": previous, "to": ctx.language, "digit": choice.digit,
                })
            else:
                await self._call_manager.record_action(ctx.call_sid, "Language Selected", {
                    "language": choice.language, "digit": choice.digit, "tries": choice.tries,
                    "no_key_pressed": choice.timed_out,
                })
        return True

    async def _save_recording(self, recorder: CallRecorder | None, call_sid: str | None) -> str | None:
        """Writes the call's WAV (off the event loop); returns its file name,
        or None if there was nothing to save or saving failed."""
        if recorder is None or not call_sid:
            return None
        if not recorder.has_audio():
            recorder.discard()
            return None
        name = f"{call_sid}.wav"
        try:
            await asyncio.to_thread(recorder.write_wav, config.RECORDINGS_DIR / name)
        except Exception:  # noqa: BLE001 - a failed recording must never break call teardown
            logger.exception("Failed to save recording for call %s", call_sid)
            return None
        return name

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
