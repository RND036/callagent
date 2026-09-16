"""In-band DTMF detection via the Goertzel algorithm, reading the caller's
raw audio directly.

Needed because SmartPBX has been observed to silently drop `dtmf` control
events entirely -- no frame at all even though the caller pressed a key and
media kept flowing. This is *not* a substitute for the PBX's own dtmf event
(in-band detection is inherently heuristic); callers should run both sources
and dedupe on the digit within a short window -- see DtmfDedupe in
call_session.py.
"""
from __future__ import annotations

import math
import struct

try:
    import audioop
except ImportError:  # Python 3.13+
    import audioop_lts as audioop  # type: ignore[import-not-found]

_SAMPLE_RATE_HZ = 8000
_BLOCK_SIZE = 205  # ~25.6ms at 8kHz, standard Goertzel window for DTMF
_CONFIRM_BLOCKS = 3  # consecutive positive blocks required before emitting (~75ms)
_GUARD_FREQ = 1000.0  # between the row/col bands; a real tone has little energy here
_ROW_FREQS = (697, 770, 852, 941)
_COL_FREQS = (1209, 1336, 1477, 1633)
_DIGIT_MAP = {
    (697, 1209): "1", (697, 1336): "2", (697, 1477): "3", (697, 1633): "A",
    (770, 1209): "4", (770, 1336): "5", (770, 1477): "6", (770, 1633): "B",
    (852, 1209): "7", (852, 1336): "8", (852, 1477): "9", (852, 1633): "C",
    (941, 1209): "*", (941, 1336): "0", (941, 1477): "#", (941, 1633): "D",
}
_MIN_ENERGY = 1.0e5  # ignore near-silence blocks
_TWIST_MAX_DB = 8.0  # max allowed row/col energy imbalance (rejects speech)


def _goertzel_power(samples: tuple[int, ...], freq: float) -> float:
    n = len(samples)
    k = int(0.5 + n * freq / _SAMPLE_RATE_HZ)
    omega = (2.0 * math.pi * k) / n
    coeff = 2.0 * math.cos(omega)
    s_prev = 0.0
    s_prev2 = 0.0
    for sample in samples:
        s = sample + coeff * s_prev - s_prev2
        s_prev2 = s_prev
        s_prev = s
    return s_prev2 * s_prev2 + s_prev * s_prev - coeff * s_prev * s_prev2


class InbandDtmfDetector:
    """Feed it caller audio in call order; it returns a digit the instant a
    keypress is confirmed (and won't re-fire until the tone stops), else None."""

    def __init__(self) -> None:
        self._buffer: list[int] = []
        self._pending: str | None = None
        self._pending_count = 0
        self._active = False

    def feed_ulaw(self, ulaw_chunk: bytes) -> str | None:
        return self.feed_pcm16(audioop.ulaw2lin(ulaw_chunk, 2))

    def feed_pcm16(self, pcm16_chunk: bytes) -> str | None:
        n_samples = len(pcm16_chunk) // 2
        self._buffer.extend(struct.unpack(f"<{n_samples}h", pcm16_chunk[: n_samples * 2]))

        digit = None
        while len(self._buffer) >= _BLOCK_SIZE:
            block = tuple(self._buffer[:_BLOCK_SIZE])
            del self._buffer[:_BLOCK_SIZE]
            result = self._process_block(block)
            if result is not None:
                digit = result  # last confirmation wins if several land in one feed()
        return digit

    def _process_block(self, block: tuple[int, ...]) -> str | None:
        row_freq, row_energy = max(((f, _goertzel_power(block, f)) for f in _ROW_FREQS), key=lambda x: x[1])
        col_freq, col_energy = max(((f, _goertzel_power(block, f)) for f in _COL_FREQS), key=lambda x: x[1])

        if row_energy < _MIN_ENERGY or col_energy < _MIN_ENERGY:
            return self._reset()

        twist_db = 10 * math.log10(max(row_energy, col_energy) / min(row_energy, col_energy))
        if twist_db > _TWIST_MAX_DB:
            return self._reset()

        # Broadband noise/speech tends to have comparable energy at a guard
        # frequency between the two DTMF bands; a real tone pair doesn't.
        # Rejects the "talk-off" false positives a bare two-frequency test lets through.
        guard_energy = _goertzel_power(block, _GUARD_FREQ)
        if guard_energy > 0 and min(row_energy, col_energy) / guard_energy < 4.0:
            return self._reset()

        digit = _DIGIT_MAP.get((row_freq, col_freq))
        if digit is None:
            return self._reset()

        if digit == self._pending:
            self._pending_count += 1
        else:
            self._pending = digit
            self._pending_count = 1

        if self._pending_count >= _CONFIRM_BLOCKS and not self._active:
            self._active = True
            return digit
        return None

    def _reset(self) -> None:
        self._pending = None
        self._pending_count = 0
        self._active = False
        return None
