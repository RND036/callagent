"""Standalone smoke-test script (not a pytest test) that behaves like
Asterisk's AudioSocket() dialplan app: opens a raw TCP connection to the
running bridge, sends a UUID packet, streams a sample PCM16 WAV as
real-time-paced audio packets (plus one DTMF packet partway through),
collects the audio packets sent back, and saves them as a playable WAV so a
human can listen to the agent's response.

Usage (with `python -m main` already running in another terminal):
    .venv/bin/python -m tests.mock_asterisk tests/fixtures/sample_call_pcm16_8k.wav out.wav
"""
import asyncio
import struct
import sys
import uuid
import wave

import config
from pbx_bridge.audiosocket_protocol import TYPE_DTMF, TYPE_TERMINATE, TYPE_UUID

_HEADER_STRUCT = struct.Struct(">BH")
FRAME_MS = 20
FRAME_BYTES = int(config.AUDIOSOCKET_SAMPLE_RATE_HZ * FRAME_MS / 1000) * 2  # PCM16: 2 bytes/sample


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


async def send_packet(writer: asyncio.StreamWriter, msg_type: int, payload: bytes) -> None:
    writer.write(_HEADER_STRUCT.pack(msg_type, len(payload)) + payload)
    await writer.drain()


async def recv_packet(reader: asyncio.StreamReader) -> tuple[int, bytes] | None:
    try:
        header = await reader.readexactly(_HEADER_STRUCT.size)
    except asyncio.IncompleteReadError:
        return None
    msg_type, length = _HEADER_STRUCT.unpack(header)
    payload = await reader.readexactly(length) if length else b""
    return msg_type, payload


async def run(in_wav: str, out_wav: str, host: str, port: int) -> None:
    frames = read_pcm16_frames(in_wav)
    received = bytearray()
    audio_type = {8000: 0x10, 12000: 0x11, 16000: 0x12}[config.AUDIOSOCKET_SAMPLE_RATE_HZ]

    reader, writer = await asyncio.open_connection(host, port)
    await send_packet(writer, TYPE_UUID, uuid.uuid4().bytes)

    async def sender():
        for i, frame in enumerate(frames):
            await send_packet(writer, audio_type, frame)
            if i == len(frames) // 2:
                await send_packet(writer, TYPE_DTMF, b"5")  # exercise mid-call DTMF handling
            await asyncio.sleep(FRAME_MS / 1000)
        # Deliberately no terminate packet here: a real caller doesn't hang
        # up the instant they stop talking -- Asterisk stays connected while
        # the agent responds. The sleep below in run() closes the socket
        # from our end instead.

    async def receiver():
        while True:
            packet = await recv_packet(reader)
            if packet is None:
                return
            msg_type, payload = packet
            if msg_type in (0x10, 0x11, 0x12, 0x13, 0x14, 0x15, 0x16, 0x17, 0x18):
                received.extend(payload)
            elif msg_type == TYPE_TERMINATE:
                return
            else:
                print(f"[control] type=0x{msg_type:02x} payload={payload!r}")

    send_task = asyncio.create_task(sender())
    recv_task = asyncio.create_task(receiver())
    await send_task
    await asyncio.sleep(8.0)  # give the agent time to finish speaking after our audio ends
    recv_task.cancel()
    try:
        await recv_task
    except asyncio.CancelledError:
        pass

    writer.close()
    await writer.wait_closed()

    write_pcm16_wav(out_wav, config.AUDIOSOCKET_SAMPLE_RATE_HZ, bytes(received))
    print(f"Wrote {len(received)} bytes of response audio to {out_wav}")


if __name__ == "__main__":
    in_wav = sys.argv[1] if len(sys.argv) > 1 else "tests/fixtures/sample_call_pcm16_8k.wav"
    out_wav = sys.argv[2] if len(sys.argv) > 2 else "tests/fixtures/response.wav"
    host = config.WS_HOST if config.WS_HOST != "0.0.0.0" else "localhost"
    asyncio.run(run(in_wav, out_wav, host, config.WS_PORT))
