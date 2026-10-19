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

import numpy as np

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


def _design_lowpass(num_taps: int, cutoff_ratio: float) -> np.ndarray:
    """Windowed-sinc low-pass FIR. `cutoff_ratio` is the cutoff as a fraction
    of the input sample rate. Normalised to unity gain at DC."""
    n = np.arange(num_taps) - (num_taps - 1) / 2
    taps = np.sinc(2 * cutoff_ratio * n) * np.blackman(num_taps)
    return taps / taps.sum()


_FILTER_TAPS = 63
_CUTOFF_FRACTION_OF_TARGET_RATE = 0.425  # of the *output* rate, i.e. just under its Nyquist


class OutboundTranscoder:
    """Gemini -> Asterisk: PCM16 @GEMINI_OUTPUT_SAMPLE_RATE_HZ -> PCM16
    @AUDIOSOCKET_SAMPLE_RATE_HZ.

    Downsampling without a low-pass filter folds everything above the target
    Nyquist (the "s"/"sh" and breath energy between 4 and 12 kHz) back into the
    phone band as harsh, metallic aliasing. So when the rates divide evenly
    the audio is low-pass filtered first, then decimated; the filter tail and
    decimation phase carry across chunks so chunked output equals one-shot
    output. Other rate ratios filter first and fall back to ratecv."""

    def __init__(self) -> None:
        src = config.GEMINI_OUTPUT_SAMPLE_RATE_HZ
        dst = config.AUDIOSOCKET_SAMPLE_RATE_HZ
        self._resample_state = None
        self._factor = src // dst if dst and src % dst == 0 else 0
        self._taps = _design_lowpass(_FILTER_TAPS, _CUTOFF_FRACTION_OF_TARGET_RATE * dst / src)
        # Pre-filled with silence so the first output is aligned with the input.
        self._tail = np.zeros(_FILTER_TAPS - 1)
        self._phase = 0  # index within the filtered stream of the next sample to keep

    def _filter(self, pcm: bytes) -> np.ndarray:
        samples = np.frombuffer(pcm[: len(pcm) - len(pcm) % _SAMPLE_WIDTH_BYTES], dtype="<i2").astype(np.float64)
        padded = np.concatenate([self._tail, samples])
        self._tail = padded[len(padded) - (_FILTER_TAPS - 1):]
        return np.convolve(padded, self._taps, mode="valid")

    @staticmethod
    def _to_pcm16(samples: np.ndarray) -> bytes:
        return np.clip(np.rint(samples), -32768, 32767).astype("<i2").tobytes()

    def transcode(self, pcm24k_chunk: bytes) -> bytes:
        filtered = self._filter(pcm24k_chunk)
        if self._factor:
            kept = filtered[self._phase :: self._factor]
            self._phase = (self._phase - len(filtered)) % self._factor
            return self._to_pcm16(kept)
        pcm_out, self._resample_state = resample_pcm16(
            self._to_pcm16(filtered),
            config.GEMINI_OUTPUT_SAMPLE_RATE_HZ,
            config.AUDIOSOCKET_SAMPLE_RATE_HZ,
            self._resample_state,
        )
        return pcm_out
