"""Turns an uploaded recording (the language-menu prompts) into what the phone line plays: 8 kHz mono 16-bit PCM.

WAV files are read with the stdlib; anything else (MP3, M4A voice memos, OGG, float WAV) goes through ffmpeg when
it is installed.
"""
from __future__ import annotations

import io
import shutil
import subprocess
import wave

try:
    import audioop
except ImportError:  # Python 3.13+
    import audioop_lts as audioop  # type: ignore[import-not-found]

from audio.codec import resample_pcm16

LINE_RATE_HZ = 8000
MAX_UPLOAD_BYTES = 15 * 1024 * 1024
MAX_SECONDS = 60.0
_SAMPLE_WIDTH_BYTES = 2
_MIN_PEAK = 200  # below this the recording is effectively silent


def ffmpeg_available() -> bool:
    return shutil.which("ffmpeg") is not None


def _decode_wav(data: bytes) -> bytes:
    with wave.open(io.BytesIO(data), "rb") as wav:
        channels, width, rate = wav.getnchannels(), wav.getsampwidth(), wav.getframerate()
        frames = wav.readframes(wav.getnframes())
    if width == 1:  # 8-bit WAV is unsigned
        frames = audioop.bias(frames, 1, -128)
    if width != _SAMPLE_WIDTH_BYTES:
        frames = audioop.lin2lin(frames, width, _SAMPLE_WIDTH_BYTES)
    if channels == 2:
        frames = audioop.tomono(frames, _SAMPLE_WIDTH_BYTES, 0.5, 0.5)
    elif channels != 1:
        raise ValueError("Only mono or stereo recordings are supported.")
    if rate != LINE_RATE_HZ:
        frames, _ = resample_pcm16(frames, rate, LINE_RATE_HZ)
    return frames


def _decode_with_ffmpeg(data: bytes) -> bytes:
    if not ffmpeg_available():
        raise ValueError("Upload a WAV file, or install ffmpeg on the server to use MP3/M4A recordings.")
    try:
        proc = subprocess.run(
            ["ffmpeg", "-v", "error", "-i", "pipe:0", "-f", "s16le", "-ac", "1", "-ar", str(LINE_RATE_HZ), "pipe:1"],
            input=data, capture_output=True, timeout=60, check=False,
        )
    except subprocess.TimeoutExpired:
        raise ValueError("The recording took too long to convert.") from None
    if proc.returncode != 0 or not proc.stdout:
        raise ValueError("That file is not a recording the server can read.")
    return proc.stdout


def to_line_pcm(data: bytes) -> bytes:
    """8 kHz mono PCM16 from an uploaded recording. Raises ValueError (with a message for the admin) when the
    upload is unusable."""
    if not data:
        raise ValueError("The file is empty.")
    if len(data) > MAX_UPLOAD_BYTES:
        raise ValueError("The file is too large (15 MB maximum).")
    pcm = b""
    if data[:4] == b"RIFF":
        try:
            pcm = _decode_wav(data)
        except (wave.Error, EOFError, audioop.error):
            pcm = b""  # compressed/float WAV: let ffmpeg try
    if not pcm:
        pcm = _decode_with_ffmpeg(data)
    pcm = pcm[: len(pcm) - len(pcm) % _SAMPLE_WIDTH_BYTES]
    if len(pcm) / (LINE_RATE_HZ * _SAMPLE_WIDTH_BYTES) > MAX_SECONDS:
        raise ValueError("The recording is too long (60 seconds maximum).")
    if not pcm or audioop.max(pcm, _SAMPLE_WIDTH_BYTES) < _MIN_PEAK:
        raise ValueError("The recording is silent.")
    return pcm


def pcm_to_wav(pcm: bytes) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(_SAMPLE_WIDTH_BYTES)
        wav.setframerate(LINE_RATE_HZ)
        wav.writeframes(pcm)
    return buf.getvalue()


def duration_seconds(pcm: bytes) -> float:
    return round(len(pcm) / (LINE_RATE_HZ * _SAMPLE_WIDTH_BYTES), 1)
