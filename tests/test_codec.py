import math
import struct

from audio.codec import (
    InboundTranscoder,
    OutboundTranscoder,
    pcm16_to_ulaw,
    resample_pcm16,
    ulaw_to_pcm16,
)


def sine_wave_pcm16(sample_rate: int, freq: float, seconds: float) -> bytes:
    n_samples = int(sample_rate * seconds)
    samples = [
        int(8000 * math.sin(2 * math.pi * freq * i / sample_rate)) for i in range(n_samples)
    ]
    return struct.pack(f"<{n_samples}h", *samples)


def test_ulaw_pcm16_round_trip_is_close() -> None:
    original = sine_wave_pcm16(8000, freq=440, seconds=0.05)
    encoded = pcm16_to_ulaw(original)
    decoded = ulaw_to_pcm16(encoded)

    assert len(decoded) == len(original)
    orig_samples = struct.unpack(f"<{len(original) // 2}h", original)
    dec_samples = struct.unpack(f"<{len(decoded) // 2}h", decoded)
    # mu-law is lossy but should stay within a small tolerance per-sample.
    max_diff = max(abs(o - d) for o, d in zip(orig_samples, dec_samples))
    assert max_diff < 500


def test_resample_upsamples_to_expected_length() -> None:
    pcm_8k = sine_wave_pcm16(8000, freq=440, seconds=0.1)
    pcm_16k, _ = resample_pcm16(pcm_8k, 8000, 16000)
    expected_samples = (len(pcm_8k) // 2) * 2
    assert abs(len(pcm_16k) // 2 - expected_samples) <= 1


def test_resample_state_continuity_matches_single_call() -> None:
    pcm_8k = sine_wave_pcm16(8000, freq=440, seconds=0.2)

    whole, _ = resample_pcm16(pcm_8k, 8000, 16000)

    mid = len(pcm_8k) // 2
    mid -= mid % 2  # keep on a sample boundary (2 bytes/sample)
    chunk_a, chunk_b = pcm_8k[:mid], pcm_8k[mid:]
    part_a, state = resample_pcm16(chunk_a, 8000, 16000, None)
    part_b, _ = resample_pcm16(chunk_b, 8000, 16000, state)
    chunked = part_a + part_b

    assert chunked == whole


def test_inbound_transcoder_produces_16k_pcm_from_8k_ulaw() -> None:
    pcm_8k = sine_wave_pcm16(8000, freq=440, seconds=0.1)
    ulaw_8k = pcm16_to_ulaw(pcm_8k)

    transcoder = InboundTranscoder()
    pcm_16k = transcoder.transcode(ulaw_8k)

    expected_samples = len(ulaw_8k) * 2  # 8k mu-law bytes == 8k samples; upsampled 2x to 16k
    assert abs(len(pcm_16k) // 2 - expected_samples) <= 2


def test_outbound_transcoder_produces_8k_ulaw_from_24k_pcm() -> None:
    pcm_24k = sine_wave_pcm16(24000, freq=440, seconds=0.1)

    transcoder = OutboundTranscoder()
    ulaw_8k = transcoder.transcode(pcm_24k)

    assert len(ulaw_8k) == (len(pcm_24k) // 2) // 3


def test_transcoders_maintain_state_across_chunks_without_error() -> None:
    pcm_8k = sine_wave_pcm16(8000, freq=440, seconds=0.5)
    ulaw_8k = pcm16_to_ulaw(pcm_8k)

    transcoder = InboundTranscoder()
    chunk_size = 160  # 20ms @ 8kHz mu-law
    total_out = 0
    for i in range(0, len(ulaw_8k), chunk_size):
        chunk = ulaw_8k[i : i + chunk_size]
        out = transcoder.transcode(chunk)
        total_out += len(out)
    assert total_out > 0
