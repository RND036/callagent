"""Real-time resampling between FreeSWITCH mod_audio_stream's PCM16 audio and
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
    """FreeSWITCH -> Gemini: PCM16 @FS_STREAM_SAMPLE_RATE_HZ -> PCM16
    @GEMINI_INPUT_SAMPLE_RATE_HZ. Keeps ratecv state across chunks so
    resampling stays phase-continuous for the life of one call. Becomes a
    pure passthrough when the two rates already match (the recommended
    default)."""

    def __init__(self) -> None:
        self._resample_state = None
        self._passthrough = config.FS_STREAM_SAMPLE_RATE_HZ == config.GEMINI_INPUT_SAMPLE_RATE_HZ

    def transcode(self, pcm_chunk: bytes) -> bytes:
        if self._passthrough:
            return pcm_chunk
        pcm_16k, self._resample_state = resample_pcm16(
            pcm_chunk, config.FS_STREAM_SAMPLE_RATE_HZ, config.GEMINI_INPUT_SAMPLE_RATE_HZ, self._resample_state
        )
        return pcm_16k


class OutboundTranscoder:
    """Gemini -> FreeSWITCH: PCM16 @GEMINI_OUTPUT_SAMPLE_RATE_HZ -> PCM16
    @FS_STREAM_SAMPLE_RATE_HZ. Keeps ratecv state across chunks for the same
    reason as InboundTranscoder."""

    def __init__(self) -> None:
        self._resample_state = None

    def transcode(self, pcm24k_chunk: bytes) -> bytes:
        pcm_out, self._resample_state = resample_pcm16(
            pcm24k_chunk, config.GEMINI_OUTPUT_SAMPLE_RATE_HZ, config.FS_STREAM_SAMPLE_RATE_HZ, self._resample_state
        )
        return pcm_out
