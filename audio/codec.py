"""Real-time resampling between Asterisk AudioSocket's PCM16 audio and
the PCM16 rates the Gemini Live API requires (16kHz in, 24kHz out).

Uses the stdlib `audioop` module, or its PyPI backport `audioop-lts` on
Python 3.13+ where the stdlib module was removed.
"""
from __future__ import annotations

try:
    import audioop
except ImportError:  # Python 3.13+
    import audioop_lts as audioop  # type: ignore[import-not-found]

import config

_SAMPLE_WIDTH_BYTES = 2  # 16-bit PCM


def resample_pcm16(pcm_bytes: bytes, from_rate: int, to_rate: int, state=None) -> tuple[bytes, object]:
    resampled, new_state = audioop.ratecv(
        pcm_bytes, _SAMPLE_WIDTH_BYTES, 1, from_rate, to_rate, state
    )
    return resampled, new_state


class InboundTranscoder:
    """Asterisk -> Gemini: PCM16 @AUDIOSOCKET_SAMPLE_RATE_HZ -> PCM16
    @GEMINI_INPUT_SAMPLE_RATE_HZ. Keeps ratecv state across chunks so
    resampling stays phase-continuous for the life of one call. A real
    up-conversion by default (8kHz telephony audio -> 16kHz), not a
    passthrough; becomes one only if AUDIOSOCKET_SAMPLE_RATE_HZ is raised to
    match GEMINI_INPUT_SAMPLE_RATE_HZ."""

    def __init__(self) -> None:
        self._resample_state = None
        self._passthrough = config.AUDIOSOCKET_SAMPLE_RATE_HZ == config.GEMINI_INPUT_SAMPLE_RATE_HZ

    def transcode(self, pcm_chunk: bytes) -> bytes:
        if self._passthrough:
            return pcm_chunk
        pcm_16k, self._resample_state = resample_pcm16(
            pcm_chunk, config.AUDIOSOCKET_SAMPLE_RATE_HZ, config.GEMINI_INPUT_SAMPLE_RATE_HZ, self._resample_state
        )
        return pcm_16k


class OutboundTranscoder:
    """Gemini -> Asterisk: PCM16 @GEMINI_OUTPUT_SAMPLE_RATE_HZ -> PCM16
    @AUDIOSOCKET_SAMPLE_RATE_HZ. Keeps ratecv state across chunks for the same
    reason as InboundTranscoder."""

    def __init__(self) -> None:
        self._resample_state = None

    def transcode(self, pcm24k_chunk: bytes) -> bytes:
        pcm_out, self._resample_state = resample_pcm16(
            pcm24k_chunk, config.GEMINI_OUTPUT_SAMPLE_RATE_HZ, config.AUDIOSOCKET_SAMPLE_RATE_HZ, self._resample_state
        )
        return pcm_out
