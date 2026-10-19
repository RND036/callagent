"""Records both sides of a phone call to one mono WAV for Call History.

Caller and agent audio arrive as separate PCM16 streams (the agent's in
bursts, paced at real time), so each side is kept as its own track anchored to
the wall clock: before a chunk is appended, the track is padded with silence
up to "now". That keeps the two sides aligned, and mixing them at the end
reproduces the conversation as it actually happened on the line.

With a `spool` path the tracks are written to disk as the call goes on
(`<spool>.caller.pcm` / `<spool>.agent.pcm`, about a second buffered), so a
crash mid-call still leaves the audio: finish_spool() turns it into the WAV on
the next start.
"""
from __future__ import annotations

import io
import time
import wave
from pathlib import Path
from typing import BinaryIO, Callable

try:
    import audioop
except ImportError:  # Python 3.13+
    import audioop_lts as audioop  # type: ignore[import-not-found]

_SAMPLE_WIDTH_BYTES = 2  # 16-bit PCM
_TRACKS = ("caller", "agent")


def _track_path(spool: Path, track: str) -> Path:
    return spool.with_name(f"{spool.name}.{track}.pcm")


def _write_mixed_wav(caller: bytes, agent: bytes, rate: int, path: Path) -> None:
    length = max(len(caller), len(agent))
    length -= length % _SAMPLE_WIDTH_BYTES
    caller = caller.ljust(length, b"\0")[:length]
    agent = agent.ljust(length, b"\0")[:length]
    mixed = audioop.add(caller, agent, _SAMPLE_WIDTH_BYTES)  # saturates, never wraps
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(_SAMPLE_WIDTH_BYTES)
        wav.setframerate(rate)
        wav.writeframes(mixed)


def _remove_spool(spool: Path) -> None:
    for track in _TRACKS:
        _track_path(spool, track).unlink(missing_ok=True)


def finish_spool(spool: Path, rate: int, wav_path: Path) -> bool:
    """Mix the tracks a call left on disk (it never finished: crash/restart) into `wav_path` and remove them.
    Returns whether there was any audio."""
    caller, agent = (_track_path(spool, t).read_bytes() if _track_path(spool, t).exists() else b"" for t in _TRACKS)
    if caller or agent:
        _write_mixed_wav(caller, agent, rate, wav_path)
    _remove_spool(spool)
    return bool(caller or agent)


class CallRecorder:
    def __init__(self, sample_rate: int, clock: Callable[[], float] = time.monotonic,
                 spool: Path | None = None) -> None:
        self._rate = sample_rate
        self._clock = clock
        self._started = clock()
        self._spool = spool
        self._caller: BinaryIO
        self._agent: BinaryIO
        if spool is None:
            self._caller, self._agent = io.BytesIO(), io.BytesIO()
        else:
            spool.parent.mkdir(parents=True, exist_ok=True)
            buffering = sample_rate * _SAMPLE_WIDTH_BYTES  # ~1 s of audio per track
            self._caller = open(_track_path(spool, "caller"), "wb", buffering=buffering)
            self._agent = open(_track_path(spool, "agent"), "wb", buffering=buffering)

    def _append(self, track: BinaryIO, pcm: bytes) -> None:
        if not pcm or track.closed:
            return
        target = int((self._clock() - self._started) * self._rate) * _SAMPLE_WIDTH_BYTES
        if (pos := track.tell()) < target:
            track.write(bytes(target - pos))
        track.write(pcm)

    def add_caller(self, pcm: bytes) -> None:
        self._append(self._caller, pcm)

    def add_agent(self, pcm: bytes) -> None:
        self._append(self._agent, pcm)

    def has_audio(self) -> bool:
        return any(not t.closed and t.tell() for t in (self._caller, self._agent))

    def _take_tracks(self) -> tuple[bytes, bytes]:
        if self._spool is None:
            return self._caller.getvalue(), self._agent.getvalue()  # type: ignore[attr-defined]
        self._caller.close()
        self._agent.close()
        return _track_path(self._spool, "caller").read_bytes(), _track_path(self._spool, "agent").read_bytes()

    def write_wav(self, path: Path) -> None:
        caller, agent = self._take_tracks()
        _write_mixed_wav(caller, agent, self._rate, path)
        if self._spool is not None:
            _remove_spool(self._spool)

    def discard(self) -> None:
        """Nothing worth keeping (or recording failed): close and remove the spool."""
        self._caller.close()
        self._agent.close()
        if self._spool is not None:
            _remove_spool(self._spool)
