"""Standalone smoke-test script (not a pytest test) that behaves like
FreeSWITCH's mod_audio_stream: opens a WS connection to the running server,
sends one metadata text frame, streams a sample PCM16 WAV as real-time-paced
binary frames, collects the streamAudio playback frames sent back, and saves
them as a playable WAV so a human can listen to the agent's response.

Usage (with `python -m main` already running in another terminal):
    .venv/bin/python -m tests.mock_freeswitch tests/fixtures/sample_call_pcm16_16k.wav out.wav
"""
import asyncio
import base64
import json
import sys
import uuid
import wave

import websockets

import config

FRAME_MS = 20
FRAME_BYTES = int(config.FS_STREAM_SAMPLE_RATE_HZ * FRAME_MS / 1000) * 2  # PCM16: 2 bytes/sample


def read_pcm16_frames(wav_path: str) -> list[bytes]:
    with wave.open(wav_path, "rb") as w:
        data = w.readframes(w.getnframes())
    return [data[i : i + FRAME_BYTES] for i in range(0, len(data), FRAME_BYTES)]


def write_pcm16_wav(out_path: str, sample_rate: int, pcm_bytes: bytes) -> None:
    with wave.open(out_path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sample_rate)
        w.writeframes(pcm_bytes)


async def run(in_wav: str, out_wav: str, uri: str) -> None:
    frames = read_pcm16_frames(in_wav)
    received = bytearray()
    call_uuid = str(uuid.uuid4())

    async with websockets.connect(uri) as ws:
        metadata = (
            f"call_uuid={call_uuid};caller_id_number=+94770000000;"
            f"destination_number=1001;sample_rate={config.FS_STREAM_SAMPLE_RATE_HZ}"
        )
        await ws.send(metadata)

        async def sender():
            for frame in frames:
                await ws.send(frame)
                await asyncio.sleep(FRAME_MS / 1000)
            # Deliberately no disconnect message here: a real caller doesn't
            # hang up the instant they stop talking -- FreeSWITCH stays
            # connected while the agent responds. The sleep below in run()
            # closes the socket from our end instead.

        async def receiver():
            try:
                async for message in ws:
                    if isinstance(message, bytes):
                        continue
                    data = json.loads(message)
                    if data.get("type") == "streamAudio":
                        received.extend(base64.b64decode(data["data"]["audioData"]))
                    else:
                        print(f"[control] {message}")
            except websockets.exceptions.ConnectionClosed:
                pass

        send_task = asyncio.create_task(sender())
        recv_task = asyncio.create_task(receiver())
        await send_task
        await asyncio.sleep(8.0)  # give the agent time to finish speaking after our audio ends
        recv_task.cancel()
        try:
            await recv_task
        except asyncio.CancelledError:
            pass

    write_pcm16_wav(out_wav, config.FS_STREAM_SAMPLE_RATE_HZ, bytes(received))
    print(f"Wrote {len(received)} bytes of response audio to {out_wav}")


if __name__ == "__main__":
    in_wav = sys.argv[1] if len(sys.argv) > 1 else "tests/fixtures/sample_call_pcm16_16k.wav"
    out_wav = sys.argv[2] if len(sys.argv) > 2 else "tests/fixtures/response.wav"
    uri = f"ws://{config.WS_HOST if config.WS_HOST != '0.0.0.0' else 'localhost'}:{config.WS_PORT}"
    asyncio.run(run(in_wav, out_wav, uri))
