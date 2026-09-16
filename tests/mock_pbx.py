"""Standalone smoke-test script (not a pytest test) that behaves like
SmartPBX: opens a WS connection to the running server, sends a start event,
streams a sample 8kHz mu-law WAV as real-time-paced base64 media events,
collects the mu-law audio streamed back, and saves it as a playable WAV so a
human can listen to the agent's response.

Usage (with `python -m main` already running in another terminal):
    .venv/bin/python -m tests.mock_pbx tests/fixtures/sample_call.wav out.wav
"""
import asyncio
import base64
import json
import struct
import sys
import uuid

import websockets

import config

FRAME_MS = 20
FRAME_BYTES = int(config.PBX_SAMPLE_RATE_HZ * FRAME_MS / 1000)  # mu-law: 1 byte/sample

# Python's stdlib `wave` module only understands PCM/float WAV data (format
# tag 1/3) and rejects compressed formats like G.711 mu-law (tag 7) even for
# reading, so both fixture loading and response saving parse/build the RIFF
# container by hand here instead.


def read_ulaw_frames(wav_path: str) -> list[bytes]:
    with open(wav_path, "rb") as f:
        raw = f.read()
    assert raw[:4] == b"RIFF" and raw[8:12] == b"WAVE", "not a WAV file"
    pos = 12
    data = b""
    while pos + 8 <= len(raw):
        chunk_id = raw[pos : pos + 4]
        (chunk_size,) = struct.unpack("<I", raw[pos + 4 : pos + 8])
        chunk_data = raw[pos + 8 : pos + 8 + chunk_size]
        if chunk_id == b"data":
            data = chunk_data
            break
        pos += 8 + chunk_size + (chunk_size % 2)  # chunks are word-aligned
    return [data[i : i + FRAME_BYTES] for i in range(0, len(data), FRAME_BYTES)]


def write_ulaw_wav(out_path: str, ulaw_bytes: bytes) -> None:
    # Minimal RIFF/WAVE header for 8-bit G.711 mu-law, mono, 8kHz (format tag 7).
    fmt_chunk = struct.pack(
        "<HHIIHH", 7, 1, config.PBX_SAMPLE_RATE_HZ, config.PBX_SAMPLE_RATE_HZ, 1, 8
    )
    data_size = len(ulaw_bytes)
    riff_size = 4 + (8 + len(fmt_chunk)) + (8 + data_size)
    with open(out_path, "wb") as f:
        f.write(b"RIFF" + struct.pack("<I", riff_size) + b"WAVE")
        f.write(b"fmt " + struct.pack("<I", len(fmt_chunk)) + fmt_chunk)
        f.write(b"data" + struct.pack("<I", data_size) + ulaw_bytes)


async def run(in_wav: str, out_wav: str, uri: str) -> None:
    frames = read_ulaw_frames(in_wav)
    received = bytearray()
    stream_sid = str(uuid.uuid4())

    async with websockets.connect(uri) as ws:
        await ws.send(
            json.dumps(
                {
                    "event": "start",
                    "start": {
                        "streamSid": stream_sid,
                        "callSid": "test-call-1",
                        "otherLegCallId": "test-leg-2",
                        "accountId": "test-account",
                        "callerIdNumber": "+94770000000",
                        "mediaFormat": {"encoding": "g711_ulaw", "sampleRate": config.PBX_SAMPLE_RATE_HZ},
                    },
                }
            )
        )

        async def sender():
            for frame in frames:
                payload = base64.b64encode(frame).decode("ascii")
                await ws.send(json.dumps({"event": "media", "media": {"payload": payload}}))
                await asyncio.sleep(FRAME_MS / 1000)
            # Deliberately no hangup here: a real caller doesn't hang up the
            # instant they stop talking -- the PBX stays connected while the
            # agent responds. The client-side sleep below closes the socket.

        async def receiver():
            try:
                async for message in ws:
                    if isinstance(message, bytes):
                        continue
                    data = json.loads(message)
                    if data.get("event") == "media":
                        received.extend(base64.b64decode(data["media"]["payload"]))
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

    write_ulaw_wav(out_wav, bytes(received))
    print(f"Wrote {len(received)} bytes of response audio to {out_wav}")


if __name__ == "__main__":
    in_wav = sys.argv[1] if len(sys.argv) > 1 else "tests/fixtures/sample_call.wav"
    out_wav = sys.argv[2] if len(sys.argv) > 2 else "tests/fixtures/response.wav"
    uri = f"ws://{config.WS_HOST if config.WS_HOST != '0.0.0.0' else 'localhost'}:{config.WS_PORT}"
    asyncio.run(run(in_wav, out_wav, uri))
