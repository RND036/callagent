"""Near-field noise gate for the caller's audio, applied before it reaches
Gemini. Someone speaking into the handset is much louder at the mic than a
TV, traffic, or other people in the room, so only audio well above the
background level is passed through; everything else is replaced with PCM16
silence.

Gated audio is replaced with silence rather than dropped so the stream stays
continuous -- Gemini's own VAD still needs to "hear" the gaps to detect end of
speech. Without this, background sound reaches Gemini's VAD directly and can
barge in on the agent or get transcribed as something the caller said.
"""
from __future__ import annotations

import logging
from collections import deque

try:
    import audioop
except ImportError:  # Python 3.13+
    import audioop_lts as audioop  # type: ignore[import-not-found]

import config

logger = logging.getLogger(__name__)

_SAMPLE_WIDTH_BYTES = 2  # 16-bit PCM

# The background level is the quietest chunk heard over this window: normal
# speech always dips between syllables within a couple of seconds, so the
# caller's own voice doesn't raise it, but a steady noise (fan, TV, traffic)
# does within ~2s.
_FLOOR_WINDOW_MS = 2000.0


class NearFieldGate:
    """Passes a chunk once its RMS level is at least `open_ratio` times the
    background level *and* at least `min_open_rms`, sustained for `attack_ms`
    (so a single click or clatter doesn't open it). Stays open through
    `hangover_ms` of quieter audio so word endings and short pauses aren't cut.
    The last `preroll_ms` of gated audio is replayed when the gate opens, so
    the start of the first word isn't clipped."""

    def __init__(
        self,
        sample_rate: int,
        *,
        open_ratio: float,
        min_open_rms: int,
        attack_ms: float,
        hangover_ms: float,
        preroll_ms: float,
    ) -> None:
        self._bytes_per_ms = sample_rate * _SAMPLE_WIDTH_BYTES / 1000
        self._open_ratio = open_ratio
        self._min_open_rms = min_open_rms
        self._attack_ms = attack_ms
        self._hangover_ms = hangover_ms
        self._preroll_ms = max(preroll_ms, attack_ms)  # attack chunks must survive to be replayed

        self._open = False
        self._loud_ms = 0.0  # consecutive loud audio while closed
        self._quiet_ms = 0.0  # consecutive quiet audio while open
        self._preroll: deque[tuple[bytes, float]] = deque()
        self._preroll_total_ms = 0.0
        self._history: deque[tuple[int, float]] = deque()  # (rms, duration_ms) for the floor window
        self._history_total_ms = 0.0

        # Per-call stats, for tuning the thresholds from real call logs.
        self.total_ms = 0.0
        self.passed_ms = 0.0
        self.peak_rms = 0

    @property
    def is_open(self) -> bool:
        return self._open

    @property
    def noise_floor(self) -> int:
        return min((rms for rms, _ in self._history), default=0)

    def threshold(self) -> float:
        return max(self._min_open_rms, self.noise_floor * self._open_ratio)

    def process(self, pcm: bytes) -> list[bytes]:
        """Returns the chunks to forward in place of `pcm`, in order: either
        one chunk (the audio, or silence of the same length) or, on the chunk
        that opens the gate, the replayed pre-roll ending with `pcm`."""
        if not pcm:
            return []
        duration_ms = len(pcm) / self._bytes_per_ms
        rms = audioop.rms(pcm, _SAMPLE_WIDTH_BYTES)
        loud = rms >= self.threshold()  # judged against the floor *before* this chunk
        self._record_level(rms, duration_ms)
        self.total_ms += duration_ms
        self.peak_rms = max(self.peak_rms, rms)

        if self._open:
            self._quiet_ms = 0.0 if loud else self._quiet_ms + duration_ms
            if self._quiet_ms < self._hangover_ms:
                self.passed_ms += duration_ms
                return [pcm]
            self._open = False
            self._loud_ms = 0.0
            logger.debug("Noise gate closed (floor=%d)", self.noise_floor)

        self._remember(pcm, duration_ms)
        self._loud_ms = self._loud_ms + duration_ms if loud else 0.0
        if loud and self._loud_ms >= self._attack_ms:
            self._open = True
            self._quiet_ms = 0.0
            logger.debug("Noise gate opened (rms=%d, threshold=%.0f)", rms, self.threshold())
            replay = [chunk for chunk, _ in self._preroll]
            self.passed_ms += self._preroll_total_ms
            self._preroll.clear()
            self._preroll_total_ms = 0.0
            return replay
        return [bytes(len(pcm))]

    def summary(self) -> str:
        passed_pct = 100 * self.passed_ms / self.total_ms if self.total_ms else 0.0
        return (
            f"passed {passed_pct:.0f}% of caller audio, background ~{self.noise_floor} RMS, "
            f"loudest {self.peak_rms} RMS (min open level {self._min_open_rms}, ratio {self._open_ratio:g})"
        )

    def _record_level(self, rms: int, duration_ms: float) -> None:
        self._history.append((rms, duration_ms))
        self._history_total_ms += duration_ms
        while self._history_total_ms - self._history[0][1] >= _FLOOR_WINDOW_MS:
            self._history_total_ms -= self._history.popleft()[1]

    def _remember(self, pcm: bytes, duration_ms: float) -> None:
        self._preroll.append((pcm, duration_ms))
        self._preroll_total_ms += duration_ms
        while self._preroll_total_ms - self._preroll[0][1] >= self._preroll_ms:
            self._preroll_total_ms -= self._preroll.popleft()[1]


def build_caller_gate() -> NearFieldGate | None:
    """One gate per call (it tracks that call's background level), or None
    when disabled via NAILAGENT_NOISE_GATE_ENABLED."""
    if not config.NOISE_GATE_ENABLED:
        return None
    return NearFieldGate(
        config.AUDIOSOCKET_SAMPLE_RATE_HZ,
        open_ratio=config.NOISE_GATE_OPEN_RATIO,
        min_open_rms=config.NOISE_GATE_MIN_RMS,
        attack_ms=config.NOISE_GATE_ATTACK_MS,
        hangover_ms=config.NOISE_GATE_HANGOVER_MS,
        preroll_ms=config.NOISE_GATE_PREROLL_MS,
    )
