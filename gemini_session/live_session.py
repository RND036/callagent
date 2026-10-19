"""The agent's part of a call: bridges a phone line (pbx_bridge.transport.AudioTransport) to a Gemini Live session.
Ported from cmc-new's app/ai/live_session.py. run_agent() does not own the line: it returns what happened and the
caller hangs up.

  - Long-lived tasks: caller audio -> Gemini, silence watchdog, hang-up after goodbye, keypad digits.
  - Connection loop: open a Live session and stream model audio/transcripts/tool calls to the line. Gemini closes a
    connection after ~10 minutes (warning first with GoAway) and sometimes drops it earlier (1006/1011); the loop then
    reconnects with the latest session-resumption handle while the caller stays on the line. Caller audio is buffered
    meanwhile, and context-window compression lets a call run past the 15-minute audio limit.
  - After a reconnect the agent carries on silently in the same voice and language: a resumed session is handed the
    turns said after its handle was issued; a fresh one (no handle, or the handle was refused) gets the whole call so
    far plus what the tools established about the caller (ctx.customer) in its system instruction.
"""
from __future__ import annotations

import asyncio
import difflib
import logging
import re
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime

from google.genai import types

import config
import languages
from gemini_session.persona import (
    DEFAULT_LANGUAGE,
    ANYTHING_ELSE_RE,
    GOODBYE_RE,
    SYSTEM_INSTRUCTION,
    build_system_instruction,
    call_recap,
    goodbye_nudge_note,
    greeting_note,
    language_switched_note,
    missed_turns_note,
    no_response_note,
    normalize_language,
    resume_note,
    still_there_note,
    wrap_up_note,
)
from gemini_session.tool_declarations import FUNCTION_DECLARATIONS
from pbx_bridge.transport import INPUT_RATE, OUTPUT_RATE, AudioTransport
from tools import registry
from tools.context import CallContext

logger = logging.getLogger(__name__)

RECONNECT_BACKOFF_SECONDS = (0.5, 1.0, 2.0, 4.0)  # waits between attempts within one outage, then the last repeats
GOODBYE_SETTLE_SECONDS = 1.5  # after end_call, no new audio for this long = the goodbye has been sent
GOODBYE_GRACE_SECONDS = 8.0
GOODBYE_NUDGE_SECONDS = 4.0
WRAP_UP_BEFORE_SECONDS = 60
GOAWAY_MARGIN_SECONDS = 2.0
BUFFER_CHUNKS = 100  # caller audio kept while reconnecting (~2-4 s)
REPEAT_SIMILAR = 0.85
REPEAT_WARN = 3
STALL_GAP_SECONDS = 3.0
BURST_REASK_SECONDS = 3.5  # a short caller sound the model never transcribed: re-ask after this long
VOICE_RECENT_SECONDS = 1.0  # never prompt over a caller who is making sound right now
SHORT_TURN_SECONDS = 1.0

# Caller-side transcriber language hints per IVR language. English stays in for Sinhala/Tamil calls since callers
# mix in English salon terms.
_START = {"low": "START_SENSITIVITY_LOW", "high": "START_SENSITIVITY_HIGH"}
_END = {"low": "END_SENSITIVITY_LOW", "high": "END_SENSITIVITY_HIGH"}


# ---------- config ----------

def _ai_settings(settings) -> tuple[str, float | None, int]:
    """(voice, temperature, silence ms) from the dashboard settings, falling back to the .env values."""
    voice = getattr(settings, "ai_voice", "") or config.GEMINI_VOICE_NAME
    raw = str(getattr(settings, "ai_temperature", "") or "").strip()
    try:
        temperature = float(raw) if raw else config.GEMINI_TEMPERATURE
    except ValueError:
        temperature = config.GEMINI_TEMPERATURE
    silence_ms = int(getattr(settings, "ai_vad_silence_ms", 0) or config.VAD_SILENCE_MS)
    return voice, temperature, silence_ms


def live_config(
    system_instruction: str | None = None, language: str = DEFAULT_LANGUAGE, handle: str | None = None,
    settings=None,
) -> types.LiveConnectConfig:
    voice, temperature, silence_ms = _ai_settings(settings)
    return types.LiveConnectConfig(
        system_instruction=system_instruction or SYSTEM_INSTRUCTION,
        response_modalities=["AUDIO"],
        speech_config=types.SpeechConfig(
            voice_config=types.VoiceConfig(
                prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name=voice)
            )
        ),
        **({"temperature": temperature} if temperature is not None else {}),
        # Only the language the caller picked in the IVR (plus English): offering all three let the transcriber
        # hear Tamil as Sinhala.
        input_audio_transcription=types.AudioTranscriptionConfig(
            language_codes=list(languages.get(normalize_language(language)).transcription_codes)
        ),
        output_audio_transcription=types.AudioTranscriptionConfig(),
        realtime_input_config=types.RealtimeInputConfig(
            turn_coverage="TURN_INCLUDES_ALL_INPUT",
            automatic_activity_detection=types.AutomaticActivityDetection(
                start_of_speech_sensitivity=_START.get(config.VAD_START_SENSITIVITY, "START_SENSITIVITY_LOW"),
                end_of_speech_sensitivity=_END.get(config.VAD_END_SENSITIVITY, "END_SENSITIVITY_LOW"),
                silence_duration_ms=silence_ms,
                prefix_padding_ms=config.VAD_PREFIX_PADDING_MS,
            ),
        ),
        tools=[{"function_declarations": FUNCTION_DECLARATIONS}],
        # What keeps the call alive past Gemini's connection and audio-length limits:
        context_window_compression=types.ContextWindowCompressionConfig(sliding_window=types.SlidingWindow()),
        session_resumption=types.SessionResumptionConfig(handle=handle),
    )


build_connect_config = live_config  # old name


async def send_note(session, text: str) -> None:
    """A bracketed instruction to the model (greeting cue, still-there prompt...). One place to switch method."""
    if config.LIVE_NOTE_MODE == "realtime_text":
        await session.send_realtime_input(text=text)
    else:
        await session.send_client_content(
            turns=types.Content(role="user", parts=[types.Part(text=text)]), turn_complete=True
        )


def parse_duration(value: str | None) -> float:
    """'12s' / '1.5s' / '500ms' -> seconds. Unknown -> 0."""
    m = re.fullmatch(r"\s*([\d.]+)\s*(ms|s)?\s*", str(value or ""))
    if not m:
        return 0.0
    n = float(m.group(1))
    return n / 1000 if m.group(2) == "ms" else n


async def build_instruction(ctx: CallContext) -> tuple[str, object]:
    """(system instruction, settings) for this call, from the salon's current settings and services."""
    kwargs = {
        "language": normalize_language(ctx.language),
        "now": datetime.now(config.SALON_TZ),
        "caller_number": ctx.caller_number,
        "language_menu_key": ctx.language_menu_key,
    }
    services = settings = None
    if ctx.store is not None:
        try:
            services = await ctx.store.list_services()
        except Exception as e:  # noqa: BLE001
            logger.warning("Failed to fetch services for session: %s", e)
    if ctx.settings_store is not None:
        try:
            settings = await ctx.settings_store.get_settings()
        except Exception as e:  # noqa: BLE001
            logger.warning("Failed to fetch dynamic settings for session: %s", e)
    faqs = None
    if getattr(ctx, "faq_store", None) is not None:
        try:
            faqs = await ctx.faq_store.list_faqs()
        except Exception as e:  # noqa: BLE001
            logger.warning("Failed to fetch FAQs for session: %s", e)
    return build_system_instruction(settings, services=services, faqs=faqs, **kwargs), settings


# ---------- per-call helpers ----------

class Silence:
    """Tracks when the caller last spoke and when the agent's audio will have finished playing."""

    def __init__(self) -> None:
        now = time.monotonic()
        self.last_caller = now
        self.agent_until = now
        self.last_audio_at = 0.0
        self.turn_audio = False
        self.awaiting_caller = False
        self.prompts = 0
        self.goodbye_text = ""
        self.heard_at = 0.0  # the gate last heard the caller's voice
        self.burst_at: float | None = None  # a sound the agent never answered, while it waited for the caller
        self.voice_since_prompt = False

    def caller_spoke(self) -> None:
        self.last_caller = time.monotonic()
        self.awaiting_caller = False
        self.prompts = 0
        self.goodbye_text = ""
        self.burst_at = None

    def caller_heard(self) -> None:
        """The gate hears the caller's voice right now. While the agent waits for an answer this may be a short
        "hello?" the model ignored: remembered, so should_prompt can re-ask. Transcription clears it."""
        now = time.monotonic()
        self.heard_at = now
        self.voice_since_prompt = True
        if self.awaiting_caller:
            self.burst_at = now

    def agent_finished(self) -> None:
        if self.turn_audio:
            self.awaiting_caller = True

    def agent_audio(self, nbytes: int) -> None:
        now = time.monotonic()
        self.agent_until = max(now, self.agent_until) + nbytes / 2 / OUTPUT_RATE
        self.last_audio_at = now
        self.burst_at = None
        self.turn_audio = True

    def interrupted(self) -> None:
        self.agent_until = time.monotonic()
        self.turn_audio = False
        self.caller_spoke()

    def silent_for(self) -> float:
        return time.monotonic() - max(self.last_caller, self.agent_until)

    def should_prompt(self, prompt_seconds: float, model_busy: bool) -> bool:
        """Only once the caller has really had their turn: the agent asked and they were quiet for `prompt_seconds`;
        the caller spoke last and the model hasn't answered: twice as long; the model is mid-turn: three times."""
        now = time.monotonic()
        if now - self.heard_at < VOICE_RECENT_SECONDS:
            return False
        if self.burst_at is not None and not model_busy and now - self.burst_at >= BURST_REASK_SECONDS \
                and now >= self.agent_until:
            return True
        factor = 3 if model_busy else 1 if self.awaiting_caller else 2
        return self.silent_for() >= prompt_seconds * factor


class LiveLink:
    """The current Live session (it changes on reconnect) plus what must survive a reconnect."""

    def __init__(self) -> None:
        self.session = None
        self.handle: str | None = None
        self.handle_turns = 0  # transcript turns the handle already holds; later ones are handed over on resume
        self.handle_misses = 0  # resume attempts with this handle that failed before the session said anything
        self.reconnects = 0
        self.alive = False  # the current session has delivered a message
        self.in_turn = False
        self.in_tool = False
        self.go_away_pending = False
        self.reconnect_needed = asyncio.Event()
        self._deadline_hit = False
        self._outage_since: float | None = None
        self._failures = 0
        self._timer: asyncio.TimerHandle | None = None
        self._buffer: deque[bytes] = deque(maxlen=BUFFER_CHUNKS)
        self._lines: deque[str] = deque(maxlen=REPEAT_WARN)

    def said(self, text: str) -> int:
        """How many times in a row the agent has now said nearly the same line."""
        line = " ".join(text.split())
        same = 1
        for earlier in reversed(self._lines):
            if difflib.SequenceMatcher(None, earlier, line).ratio() < REPEAT_SIMILAR:
                break
            same += 1
        self._lines.append(line)
        return same

    async def attach(self, session) -> None:
        self.session = session
        self.in_turn = False
        self.in_tool = False
        self.go_away_pending = False
        self._deadline_hit = False
        self.reconnect_needed.clear()
        while self._buffer:
            await session.send_realtime_input(
                audio=types.Blob(data=self._buffer.popleft(), mime_type=f"audio/pcm;rate={INPUT_RATE}")
            )

    def detach(self) -> None:
        self.session = None
        if self._timer:
            self._timer.cancel()
            self._timer = None

    async def send_audio(self, chunk: bytes) -> None:
        session = self.session
        if session is None:
            self._buffer.append(chunk)
            return
        try:
            await session.send_realtime_input(audio=types.Blob(data=chunk, mime_type=f"audio/pcm;rate={INPUT_RATE}"))
        except Exception:  # noqa: BLE001 - the connection is going away; the model loop notices and reconnects
            self._buffer.append(chunk)

    async def note(self, text: str) -> bool:
        if self.session is None:
            return False
        try:
            await send_note(self.session, text)
            return True
        except Exception:  # noqa: BLE001
            logger.warning("could not send note to the model", exc_info=True)
            return False

    def got_message(self) -> None:
        """The session works: a later drop starts a new outage with the full reconnect window."""
        self.alive = True
        self._outage_since = None
        self._failures = 0

    def failed(self) -> float | None:
        """The connection dropped: how long to wait before reconnecting, or None once this outage has lasted
        longer than config.RECONNECT_WINDOW_SECONDS."""
        now = time.monotonic()
        if self._outage_since is None:
            self._outage_since = now
        elif now - self._outage_since >= config.RECONNECT_WINDOW_SECONDS:
            return None
        self._failures += 1
        return RECONNECT_BACKOFF_SECONDS[min(self._failures, len(RECONNECT_BACKOFF_SECONDS)) - 1]

    def go_away(self, time_left: str | None) -> None:
        """Gemini will close this connection soon: reconnect at the next pause, or just before the deadline."""
        self.go_away_pending = True
        delay = max(0.0, parse_duration(time_left) - GOAWAY_MARGIN_SECONDS)
        if self._timer is None:
            self._timer = asyncio.get_running_loop().call_later(delay, self._deadline)
        if not self.in_turn:
            self.reconnect_needed.set()

    def _deadline(self) -> None:
        # Never in the middle of a tool call: the tool would run but the model would never hear its result.
        self._deadline_hit = True
        if not self.in_tool:
            self.reconnect_needed.set()

    def tool_done(self) -> None:
        self.in_tool = False
        if self._deadline_hit:
            self.reconnect_needed.set()

    def turn_done(self) -> None:
        self.in_turn = False
        if self.go_away_pending:
            self.reconnect_needed.set()


class TurnAudio:
    """What one agent turn sent, for the log: tells apart no audio, and audio that arrived too slowly to play
    without a gap (time spent in a tool call doesn't count)."""

    def __init__(self, clock=time.monotonic) -> None:
        self._clock = clock
        self.reset()

    def reset(self) -> None:
        self.started: float | None = None
        self.first_audio: float | None = None
        self.play_until: float | None = None
        self.seconds = self.gap = 0.0
        self.chars = 0

    def _now(self) -> float:
        now = self._clock()
        if self.started is None:
            self.started = now
        return now

    def text(self, text: str) -> None:
        self._now()
        self.chars += len(text.strip())

    def audio(self, pcm: bytes) -> None:
        now = self._now()
        length = len(pcm) / 2 / OUTPUT_RATE
        if self.first_audio is None:
            self.first_audio = now
        if self.play_until is not None and now > self.play_until:
            self.gap = max(self.gap, now - self.play_until)
        self.play_until = max(now, self.play_until or now) + length
        self.seconds += length

    def tool_call(self) -> None:
        self.play_until = None

    def end(self, how: str) -> tuple[str, bool] | None:
        """(log line, whether it looks broken), or None if the turn said nothing."""
        if self.started is None:
            return None
        now = self._clock()
        if self.play_until is not None:
            self.gap = max(self.gap, now - self.play_until)
        elif self.first_audio is None:
            self.gap = max(self.gap, now - self.started)
        first = f"{self.first_audio - self.started:.1f} s" if self.first_audio is not None else "never"
        line = (f"{self.seconds:.1f} s audio, first audio after {first}, longest gap {self.gap:.1f} s, "
                f"{self.chars} chars, {how}")
        broken = (self.gap >= STALL_GAP_SECONDS or (self.chars > 0 and self.seconds == 0)
                  or (how == "complete" and self.chars > 0 and self.seconds < SHORT_TURN_SECONDS))
        self.reset()
        return line, broken


# ---------- tasks ----------

async def _pump_caller_audio(line: AudioTransport, link: LiveLink, ctx: CallContext, silence: Silence) -> None:
    while True:
        chunk = await line.receive()
        if chunk is None:
            return
        if ctx.winding_down:
            continue  # keep reading so a hang-up still ends the call, but the model must not hear "thanks, bye"
        if getattr(line, "gate_open", False):  # the caller is talking: don't ask "are you still there?" over them
            silence.caller_heard()
        await link.send_audio(chunk)


async def _pump_dtmf(line: AudioTransport, link: LiveLink, ctx: CallContext) -> None:
    while True:
        digit = await line.dtmf.get()
        if ctx.winding_down:
            continue
        logger.info("DTMF digit: %s", digit)
        if ctx.language_menu_key and digit == ctx.language_menu_key:
            while link.in_tool:  # never in the middle of a tool call: it would run and the model never hear the result
                await asyncio.sleep(0.05)
            await line.clear()
            return  # ends this task: run_agent stops with "language_menu" and the caller gets the menu
        await line.clear()  # barge-in: stop what is playing
        await link.note(f"[caller pressed keypad digit: {digit}]")


async def _pump_model(session, line: AudioTransport, link: LiveLink, ctx: CallContext, transcript,
                      silence: Silence) -> None:
    turn = TurnAudio()

    def turn_over(how: str) -> None:
        if (ended := turn.end(how)) is not None:
            text, broken = ended
            (logger.warning if broken else logger.info)("call %s agent turn: %s", ctx.call_sid, text)

    # After end_call the model often makes one more turn that holds only a placeholder ("<no speech>{pause}") and
    # speaks it aloud. "goodbye": the goodbye is still to come, so let this one turn through; "mute": drop it all.
    closing: str | None = None

    while True:
        async for msg in session.receive():
            link.got_message()
            if msg.session_resumption_update:
                upd = msg.session_resumption_update
                if upd.resumable and upd.new_handle:
                    link.handle = upd.new_handle
                    link.handle_turns = transcript.turns_so_far()
                    link.handle_misses = 0
            if msg.go_away:
                logger.info("call %s: GoAway, time left %s", ctx.call_sid, msg.go_away.time_left)
                link.go_away(msg.go_away.time_left)

            sc = msg.server_content
            if sc:
                if sc.input_transcription and sc.input_transcription.text:
                    await transcript.add("caller", sc.input_transcription.text)
                    ctx.caller_said(sc.input_transcription.text)
                    logger.info("Caller said: %s", sc.input_transcription.text)
                    if sc.input_transcription.text.strip():
                        silence.caller_spoke()
                muted = closing == "mute"
                if msg.data and not muted:
                    link.in_turn = True
                    silence.agent_audio(len(msg.data))
                    turn.audio(msg.data)
                    await line.send(msg.data)
                if sc.output_transcription and sc.output_transcription.text and not muted:
                    await transcript.add("ai", sc.output_transcription.text)
                    turn.text(sc.output_transcription.text)
                    logger.info("Agent response: %s", sc.output_transcription.text)
                if sc.turn_complete and closing == "goodbye":
                    closing = "mute"
                # After end_call nothing cuts the goodbye.
                if sc.interrupted and not ctx.winding_down:
                    logger.info("Barge-in: agent speech interrupted by caller")
                    silence.interrupted()
                    await line.clear()
                    await transcript.flush()
                    await line.event({"type": "interrupted"})
                    link.turn_done()
                    turn_over("interrupted")
                if sc.turn_complete:
                    agent_text = transcript.pending("ai")
                    await transcript.flush()
                    if agent_text and not ctx.winding_down:
                        silence.goodbye_text = agent_text if GOODBYE_RE.search(agent_text) else ""
                        if (times := link.said(agent_text)) >= REPEAT_WARN:
                            logger.warning("call %s: the agent said nearly the same line %s times in a row: %r",
                                           ctx.call_sid, times, agent_text[:120])
                    if not ctx.winding_down:
                        silence.agent_finished()
                        silence.turn_audio = False
                    await line.event({"type": "turn_complete"})
                    link.turn_done()
                    turn_over("complete")

            if msg.tool_call and msg.tool_call.function_calls:
                spoken = transcript.pending("ai")  # before the flush: what the agent said in this turn so far
                await transcript.flush()
                link.in_turn = True
                link.in_tool = True
                turn.tool_call()
                responses = []
                for fc in msg.tool_call.function_calls:
                    args = dict(fc.args or {})
                    # end_call hands the model no goodbye text: note whether it already said one this turn.
                    ctx.said_goodbye = bool(GOODBYE_RE.search(spoken))
                    if ANYTHING_ELSE_RE.search(spoken):  # stays set until the caller answers (CallContext.caller_said)
                        ctx.asked_anything_else = True
                    logger.info("Tool call: %s(%s)", fc.name, args)
                    result = await registry.dispatch(fc.name, args, ctx)
                    responses.append(types.FunctionResponse(id=fc.id, name=fc.name, response=result))
                    await transcript.note("tool", f"{fc.name}({args}) -> {str(result)[:300]}")
                    if ctx.remember(fc.name, args, result) and ctx.call_manager and ctx.call_sid:
                        await ctx.call_manager.update_customer(ctx.call_sid, ctx.customer)
                await session.send_tool_response(function_responses=responses)
                link.tool_done()
                if ctx.hangup_requested:
                    closing = "mute" if ctx.said_goodbye else "goodbye"
                    ctx.ended_event.set()


async def _hang_up_after_goodbye(ctx: CallContext, silence: Silence) -> None:
    """After end_call, return once the goodbye audio has been sent: closing the line then plays it out."""
    await ctx.ended_event.wait()
    start = time.monotonic()
    while time.monotonic() - start < GOODBYE_GRACE_SECONDS:
        heard_after = silence.last_audio_at > start
        if (heard_after or ctx.said_goodbye) and silence.turn_audio and \
                time.monotonic() - silence.last_audio_at >= GOODBYE_SETTLE_SECONDS:
            return
        await asyncio.sleep(0.1)
    logger.info("call %s: no goodbye audio after end_call, hanging up", ctx.call_sid)


async def _silence_watchdog(link: LiveLink, ctx: CallContext, silence: Silence, deadline: float) -> None:
    """Nudge the model when the line goes quiet, wrap up before the time limit, and make sure a goodbye ends the
    call even if the model forgets end_call. Returns when the call should end."""
    prompt_seconds = config.SILENCE_CHECKIN_TIMEOUT_SECONDS
    max_prompts = max(1, config.SILENCE_CHECKINS_BEFORE_HANGUP - 1)
    loop = asyncio.get_running_loop()
    wrapped_up = False
    while True:
        await asyncio.sleep(0.5)
        if ctx.winding_down or link.session is None:
            continue
        if not wrapped_up and deadline - loop.time() <= WRAP_UP_BEFORE_SECONDS:
            wrapped_up = await link.note(wrap_up_note())
            continue
        if silence.goodbye_text and silence.silent_for() >= GOODBYE_NUDGE_SECONDS:
            text, silence.goodbye_text = silence.goodbye_text, ""
            logger.info("call %s: goodbye without end_call, nudging the model", ctx.call_sid)
            await link.note(goodbye_nudge_note())
            await asyncio.sleep(GOODBYE_GRACE_SECONDS)
            if not ctx.hangup_requested and not silence.goodbye_text and \
                    time.monotonic() - silence.last_caller > GOODBYE_GRACE_SECONDS:
                logger.info("call %s: still no end_call after goodbye (%r), hanging up", ctx.call_sid, text[:60])
                ctx.hangup_requested, ctx.end_reason = True, "caller_goodbye"
                return
            continue
        if not silence.should_prompt(prompt_seconds, link.in_turn):
            continue
        if silence.prompts < max_prompts or silence.voice_since_prompt:
            # Sound without words since the last prompt: the caller is there but may not hear us, ask again.
            silence.prompts = min(silence.prompts + 1, max_prompts)
            silence.voice_since_prompt = False
            silence.burst_at = None
            silence.last_caller = time.monotonic()
            silence.awaiting_caller = True
            logger.info("call %s: caller silent, still-there prompt %s", ctx.call_sid, silence.prompts)
            await link.note(still_there_note())
            continue
        logger.info("call %s: no response after %s prompts, ending call", ctx.call_sid, silence.prompts)
        await link.note(no_response_note())
        await asyncio.sleep(GOODBYE_GRACE_SECONDS)
        if not ctx.hangup_requested:
            ctx.hangup_requested, ctx.end_reason = True, "caller_silent"
            return
        # end_call arrived: _hang_up_after_goodbye finishes the call once the goodbye has played


RETRYABLE_CLOSE_CODES = {1000, 1001, 1006, 1011, 1012, 1013, 1014}


def _retryable(exc: BaseException) -> bool:
    """Connection drops are worth a reconnect; bad config / auth / quota errors (1007/1008, 4xx) are not."""
    from google.genai import errors
    from websockets.exceptions import ConnectionClosed

    if isinstance(exc, errors.APIError):
        return exc.code in RETRYABLE_CLOSE_CODES or 500 <= (exc.code or 0) < 600
    if isinstance(exc, ConnectionClosed):
        code = exc.rcvd.code if exc.rcvd else 1006
        return code in RETRYABLE_CLOSE_CODES
    return isinstance(exc, (ConnectionError, TimeoutError))


class _CallSideError(Exception):
    """An error from the caller's side (line / watchdog): ends the call, never triggers a reconnect."""

    def __init__(self, inner: BaseException) -> None:
        super().__init__(str(inner))
        self.inner = inner


# ---------- the agent ----------

@dataclass
class AgentResult:
    reason: str = ""
    error: str | None = None
    reconnects: int = 0
    greeted: bool = False


async def run_agent(line: AudioTransport, ctx: CallContext, transcript, client, model: str,
                    max_seconds: float | None = None, language_changed: bool = False) -> AgentResult:
    """Talk to the caller until the call ends. The line must already be listening. `transcript` is a
    data.call_manager.TranscriptLog; `client` a genai.Client."""
    max_seconds = max_seconds or config.MAX_CALL_SECONDS
    loop = asyncio.get_running_loop()
    deadline = loop.time() + max_seconds
    instruction, settings = await build_instruction(ctx)
    lang = normalize_language(ctx.language)
    silence = Silence()
    link = LiveLink()
    result = AgentResult(greeted=language_changed)  # a language change continues the call: recap, no greeting

    upstream = asyncio.create_task(_pump_caller_audio(line, link, ctx, silence), name="caller-audio")
    watchdog = asyncio.create_task(_silence_watchdog(link, ctx, silence, deadline), name="silence-watchdog")
    hang_up = asyncio.create_task(_hang_up_after_goodbye(ctx, silence), name="hang-up")
    keypad = asyncio.create_task(_pump_dtmf(line, link, ctx), name="keypad")
    background = {upstream, watchdog, hang_up, keypad}

    def call_side_ended(done: set) -> None:
        """A background task finished: the call ends (an error there never triggers a reconnect)."""
        for t in done:
            if (exc := t.exception()) is not None:
                raise _CallSideError(exc)
        if keypad in done and not ctx.hangup_requested:
            result.reason = "language_menu"
        elif upstream in done and not ctx.hangup_requested:
            result.reason = "caller_hung_up"

    try:
        while True:
            if done := {t for t in background if t.done()}:  # e.g. the caller hung up while we were reconnecting
                call_side_ended(done)
                break
            fresh = link.handle is None
            link.alive = False
            try:
                await transcript.flush()
                recap = call_recap(transcript.turns, ctx.customer, lang) if result.greeted else ""
                async with client.aio.live.connect(
                    model=model, config=live_config(instruction + recap, lang, link.handle, settings)
                ) as session:
                    await link.attach(session)
                    if link.reconnects:
                        logger.info("call %s: Live reconnected (#%d, resumed=%s)", ctx.call_sid, link.reconnects,
                                    not fresh)
                    if language_changed and not link.reconnects:  # first connect after the caller changed language
                        await send_note(session, language_switched_note(lang))
                    elif not result.greeted or not transcript.turns:  # nothing said yet: (re)start with the greeting
                        await send_note(session, greeting_note(settings, lang))
                        result.greeted = True
                    elif fresh:
                        await send_note(session, resume_note(lang))
                    elif missed := missed_turns_note(transcript.turns[link.handle_turns:], lang):
                        await send_note(session, missed)
                    downstream = asyncio.create_task(_pump_model(session, line, link, ctx, transcript, silence),
                                                     name="model")
                    renew = asyncio.create_task(link.reconnect_needed.wait())
                    done, _ = await asyncio.wait(
                        {downstream, renew, *background}, timeout=max(0.0, deadline - loop.time()),
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                    link.detach()
                    for t in (downstream, renew):
                        if t not in done:
                            t.cancel()
                    await asyncio.gather(downstream, renew, return_exceptions=True)

                    if done & background:
                        call_side_ended(done & background)
                        break
                    if not done:
                        logger.warning("call %s hit the %ss limit", ctx.call_sid, max_seconds)
                        result.reason = "time_limit"
                        break
                    if renew in done:
                        link.reconnects += 1
                        logger.info("call %s: renewing the Live connection (resume=%s)", ctx.call_sid,
                                    bool(link.handle))
                        continue
                    exc = downstream.exception() if downstream in done and not downstream.cancelled() else None
                    if exc is not None:
                        raise exc
                    raise ConnectionError("Live session closed")
            except _CallSideError:
                raise
            except Exception as exc:  # noqa: BLE001
                if ctx.hangup_requested:  # the call was ending anyway: the queued goodbye still plays out
                    logger.info("call %s: Live connection closed after end_call (%s)", ctx.call_sid, exc)
                    break
                if link.handle and not link.alive:
                    link.handle_misses += 1
                if link.handle and not link.alive and (not _retryable(exc) or link.handle_misses >= 2):
                    # The handle doesn't work (refused, or failing again): start over with the whole call in the
                    # recap.
                    logger.warning("call %s: Live session could not be resumed (%s), reconnecting fresh",
                                   ctx.call_sid, exc)
                    link.handle = None
                    if not _retryable(exc):
                        link.reconnects += 1
                        continue
                wait = link.failed() if _retryable(exc) else None
                if wait is None:
                    raise
                link.reconnects += 1
                logger.warning("call %s: Live connection dropped (%s), reconnecting in %.1f s (resume=%s)",
                               ctx.call_sid, exc, wait, bool(link.handle))
                await asyncio.wait(background, timeout=wait)
    except Exception as exc:  # noqa: BLE001 - any failure must still end the agent cleanly
        if isinstance(exc, _CallSideError):
            exc = exc.inner
        logger.exception("call %s: agent failed", ctx.call_sid, exc_info=exc)
        result.error = str(exc) or exc.__class__.__name__
        result.reason = "error"
    finally:
        for t in background:
            t.cancel()
        await asyncio.gather(*background, return_exceptions=True)
        link.detach()

    await transcript.flush()
    result.reason = ctx.end_reason or result.reason or "completed"
    result.reconnects = link.reconnects
    return result
