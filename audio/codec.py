"""Real-time transcoding between the PBX's G.711 mu-law 8kHz audio and the
PCM16 rates the Gemini Live API requires (16kHz in, 24kHz out).

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


def ulaw_to_pcm16(ulaw_bytes: bytes) -> bytes:
    return audioop.ulaw2lin(ulaw_bytes, _SAMPLE_WIDTH_BYTES)


def pcm16_to_ulaw(pcm_bytes: bytes) -> bytes:
    return audioop.lin2ulaw(pcm_bytes, _SAMPLE_WIDTH_BYTES)


def resample_pcm16(pcm_bytes: bytes, from_rate: int, to_rate: int, state=None) -> tuple[bytes, object]:
    resampled, new_state = audioop.ratecv(
        pcm_bytes, _SAMPLE_WIDTH_BYTES, 1, from_rate, to_rate, state
    )
    return resampled, new_state


class InboundTranscoder:
    """PBX -> Gemini: mu-law @8kHz -> PCM16 @16kHz. Keeps ratecv state across
    chunks so resampling stays phase-continuous for the life of one call."""

    def __init__(self) -> None:
        self._resample_state = None

    def transcode(self, ulaw_chunk: bytes) -> bytes:
        pcm_8k = ulaw_to_pcm16(ulaw_chunk)
        pcm_16k, self._resample_state = resample_pcm16(
            pcm_8k, config.PBX_SAMPLE_RATE_HZ, config.GEMINI_INPUT_SAMPLE_RATE_HZ, self._resample_state
        )
        return pcm_16k


class OutboundTranscoder:
    """Gemini -> PBX: PCM16 @24kHz -> mu-law @8kHz. Keeps ratecv state across
    chunks for the same reason as InboundTranscoder."""

    def __init__(self) -> None:
        self._resample_state = None

    def transcode(self, pcm24k_chunk: bytes) -> bytes:
        pcm_8k, self._resample_state = resample_pcm16(
            pcm24k_chunk, config.GEMINI_OUTPUT_SAMPLE_RATE_HZ, config.PBX_SAMPLE_RATE_HZ, self._resample_state
        )
        return pcm16_to_ulaw(pcm_8k)
