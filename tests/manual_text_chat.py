"""Manual Phase-2 validation script (not a pytest test): drives the Gemini
Live session in TEXT mode (no audio plumbing needed yet) to confirm the
system instruction + tool-calling loop works against the real API.

Usage:
    GEMINI_API_KEY=... .venv/bin/python -m tests.manual_text_chat
"""
import asyncio
import logging

from google import genai

import config
from data.json_store import JsonBookingStore
from tools.context import CallContext

logging.basicConfig(level=logging.INFO)

SCRIPTED_UTTERANCES = [
    "Hi, what services do you offer?",
    "How much is a gel manicure and how long does it take?",
    "I'd like to book a pedicure for 2026-10-01, do you have anything available?",
    "Great, book me the 10:00 slot. My name is Jane Doe, phone 555-1234.",
    "Actually, can you leave an urgent message for the owner? Tell them Jane called about a refund.",
]


async def main() -> None:
    if not config.GEMINI_API_KEY:
        raise SystemExit("Set GEMINI_API_KEY before running this script.")

    store = JsonBookingStore(
        services_path=config.SEED_SERVICES_PATH,
        bookings_path=config.BOOKINGS_PATH,
        messages_path=config.MESSAGES_PATH,
        business_hours_start=config.BUSINESS_HOURS_START,
        business_hours_end=config.BUSINESS_HOURS_END,
        slot_increment_minutes=config.SLOT_INCREMENT_MINUTES,
    )
    client = genai.Client(api_key=config.GEMINI_API_KEY)
    ctx = CallContext(
        store=store, mcp=None, call_sid="manual-test", other_leg_call_id=None, account_id=None, caller_number=None
    )

    from google.genai import types

    from gemini_session.live_session import build_instruction, live_config
    from tools import registry

    instruction, _ = await build_instruction(ctx)
    cfg = live_config(instruction)
    cfg.response_modalities = ["TEXT"]
    cfg.input_audio_transcription = cfg.output_audio_transcription = None
    async with client.aio.live.connect(model=config.GEMINI_MODEL, config=cfg) as session:
        for utterance in SCRIPTED_UTTERANCES:
            print(f"\n>>> CALLER: {utterance}")
            ctx.caller_said(utterance)  # what the live transcription feeds the booking read-back check
            await session.send_client_content(
                turns=types.Content(role="user", parts=[types.Part(text=utterance)]), turn_complete=True
            )
            reply = ""
            async for msg in session.receive():
                if msg.tool_call:
                    out = []
                    for fc in msg.tool_call.function_calls:
                        print(f"    [tool call: {fc.name}({fc.args})]")
                        out.append(types.FunctionResponse(
                            id=fc.id, name=fc.name, response=await registry.dispatch(fc.name, dict(fc.args or {}), ctx)))
                    await session.send_tool_response(function_responses=out)
                if msg.text:
                    reply += msg.text
                if msg.server_content and msg.server_content.turn_complete:
                    break
            print(f"<<< AGENT: {reply}")


if __name__ == "__main__":
    asyncio.run(main())
