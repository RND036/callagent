"""Thin wrapper around the Gemini Live API session: connects, forwards audio
or text in, and runs the receive loop -- including dispatching tool calls to
tools/registry.py and sending results back -- while yielding audio/text output
to the caller.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import AsyncIterator

from google import genai
from google.genai import types

from gemini_session.persona import SYSTEM_INSTRUCTION
from gemini_session.tool_declarations import FUNCTION_DECLARATIONS
from tools import registry
from tools.context import CallContext

logger = logging.getLogger(__name__)


@dataclass
class LiveEvent:
    audio: bytes | None = None
    text: str | None = None
    turn_complete: bool = False
    interrupted: bool = False
    tool_calls: list[str] = field(default_factory=list)  # names, for logging/tests


def build_connect_config() -> types.LiveConnectConfig:
    return types.LiveConnectConfig(
        system_instruction=SYSTEM_INSTRUCTION,
        response_modalities=["AUDIO"],
        speech_config=types.SpeechConfig(
            voice_config=types.VoiceConfig(
                prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name="Aoede")
            )
        ),
        input_audio_transcription=types.AudioTranscriptionConfig(),
        output_audio_transcription=types.AudioTranscriptionConfig(),
        realtime_input_config=types.RealtimeInputConfig(
            turn_coverage="TURN_INCLUDES_ALL_INPUT",
            automatic_activity_detection=types.AutomaticActivityDetection(
                start_of_speech_sensitivity="START_SENSITIVITY_LOW",
                end_of_speech_sensitivity="END_SENSITIVITY_LOW",
                silence_duration_ms=1000,
                prefix_padding_ms=300,
            ),
        ),
        tools=[{"function_declarations": FUNCTION_DECLARATIONS}],
    )


class GeminiLiveSession:
    """One instance per phone call (or per test conversation)."""

    def __init__(self, client: genai.Client, model: str, ctx: CallContext):
        self._client = client
        self._model = model
        self._ctx = ctx
        self._session_cm = None
        self._session = None
        self._send_queue: asyncio.Queue[bytes | None] = asyncio.Queue(maxsize=1500)
        self._send_task: asyncio.Task | None = None
        self._closed = False

    async def __aenter__(self) -> "GeminiLiveSession":
        config = build_connect_config()
        self._session_cm = self._client.aio.live.connect(model=self._model, config=config)
        self._session = await self._session_cm.__aenter__()
        self._send_task = asyncio.create_task(self._send_loop())
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        self._closed = True
        if self._send_task is not None:
            self._send_task.cancel()
            try:
                await self._send_task
            except asyncio.CancelledError:
                pass
        if self._session_cm is not None:
            await self._session_cm.__aexit__(exc_type, exc, tb)

    async def _send_loop(self) -> None:
        """Continuously drain audio chunks from the queue and send to Gemini.

        Batches up to _MAX_BATCH_CHUNKS at a time to keep each send under ~200ms
        of audio -- oversized blobs can degrade Gemini's real-time recognition.
        """
        _MAX_BATCH_CHUNKS = 10
        while not self._closed:
            chunk = await self._send_queue.get()
            if chunk is None:
                break
            chunks = [chunk]
            while len(chunks) < _MAX_BATCH_CHUNKS and not self._send_queue.empty():
                try:
                    extra = self._send_queue.get_nowait()
                    if extra is None:
                        break
                    chunks.append(extra)
                except asyncio.QueueEmpty:
                    break
            data = b"".join(chunks) if len(chunks) > 1 else chunk
            try:
                await self._session.send_realtime_input(
                    audio=types.Blob(data=data, mime_type="audio/pcm;rate=16000")
                )
            except Exception as exc:  # noqa: BLE001
                if not self._closed:
                    logger.debug("Error forwarding audio to Gemini: %s", exc)

    def send_audio_chunk_nowait(self, pcm16_bytes: bytes) -> None:
        if self._closed:
            return
        try:
            self._send_queue.put_nowait(pcm16_bytes)
        except asyncio.QueueFull:
            # Queue overflow — drop the OLDEST chunks to make room while
            # logging so we can spot sustained backpressure in production.
            dropped = 0
            while dropped < 5:  # free a small burst, not the entire queue
                try:
                    self._send_queue.get_nowait()
                    dropped += 1
                except asyncio.QueueEmpty:
                    break
            if dropped:
                logger.warning("Audio send queue full – dropped %d oldest chunks", dropped)
            try:
                self._send_queue.put_nowait(pcm16_bytes)
            except asyncio.QueueFull:
                pass  # still full after dropping; skip this chunk

    async def send_audio_chunk(self, pcm16_bytes: bytes, sample_rate_hz: int = 16000) -> None:
        self.send_audio_chunk_nowait(pcm16_bytes)

    async def send_text(self, text: str) -> None:
        await self._session.send_client_content(
            turns=types.Content(role="user", parts=[types.Part(text=text)]),
            turn_complete=True,
        )

    async def trigger_greeting(self) -> None:
        """Prompt the model to greet the caller immediately in Sinhala.

        Uses send_client_content (not send_realtime_input) so the prompt is
        treated as a system-level directive rather than something the caller
        said — avoids polluting Gemini's understanding of the conversation.
        """
        await self._session.send_client_content(
            turns=types.Content(
                role="user",
                parts=[types.Part(text="(The call has just connected. Greet the caller in a warm, cheerful, and friendly tone in Sinhala only: 'ආයුබෝවන්! Classy Nail Spa වෙත සාදරයෙන් පිළිගනිමු. මම ඔබට කොහොමද උදව් කරන්නේ?')")],
            ),
            turn_complete=True,
        )

    async def end_audio_stream(self) -> None:
        await self._session.send_realtime_input(audio_stream_end=True)

    async def events(self) -> AsyncIterator[LiveEvent]:
        """Runs the receive loop across all turns of the call, dispatching tool
        calls internally, and yielding LiveEvents until the session closes."""
        while not self._closed:
            try:
                async for message in self._session.receive():
                    if self._closed:
                        return
                    event = LiveEvent()

                    if message.server_content is not None:
                        sc = message.server_content
                        if message.data:
                            event.audio = message.data
                        if sc.input_transcription and sc.input_transcription.text:
                            logger.info("Caller said: %s", sc.input_transcription.text)
                        if sc.output_transcription and sc.output_transcription.text:
                            event.text = sc.output_transcription.text
                            logger.info("Agent response: %s", sc.output_transcription.text)
                        event.turn_complete = bool(sc.turn_complete)
                        event.interrupted = bool(sc.interrupted)
                        if event.interrupted:
                            logger.info("Agent was interrupted by caller")

                    if message.tool_call is not None and message.tool_call.function_calls:
                        function_responses = []
                        for fc in message.tool_call.function_calls:
                            logger.info("Tool call: %s(%s)", fc.name, fc.args)
                            result = await registry.dispatch(fc.name, fc.args or {}, self._ctx)
                            function_responses.append(
                                types.FunctionResponse(id=fc.id, name=fc.name, response=result)
                            )
                            event.tool_calls.append(fc.name)
                        await self._session.send_tool_response(function_responses=function_responses)

                    if event.audio or event.text or event.turn_complete or event.interrupted or event.tool_calls:
                        yield event
            except asyncio.CancelledError:
                break
            except Exception as exc:  # noqa: BLE001
                if self._closed:
                    break
                logger.debug("Receive loop turn reset: %s", exc)
                await asyncio.sleep(0.01)
