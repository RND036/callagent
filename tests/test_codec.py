import math
import struct

import config
from audio.codec import InboundTranscoder, OutboundTranscoder, resample_pcm16


def sine_wave_pcm16(sample_rate: int, freq: float, seconds: float) -> bytes:
    n_samples = int(sample_rate * seconds)
    samples = [
        int(8000 * math.sin(2 * math.pi * freq * i / sample_rate)) for i in range(n_samples)
    ]
    return struct.pack(f"<{n_samples}h", *samples)


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


def test_inbound_transcoder_upsamples_8k_to_16k_by_default() -> None:
    assert config.AUDIOSOCKET_SAMPLE_RATE_HZ == 8000
    assert config.GEMINI_INPUT_SAMPLE_RATE_HZ == 16000

    pcm_in = sine_wave_pcm16(config.AUDIOSOCKET_SAMPLE_RATE_HZ, freq=440, seconds=0.1)
    transcoder = InboundTranscoder()
    pcm_out = transcoder.transcode(pcm_in)

    expected_samples = (len(pcm_in) // 2) * 2  # 8k -> 16k doubles the sample count
    assert abs(len(pcm_out) // 2 - expected_samples) <= 1


def test_outbound_transcoder_produces_pcm_at_audiosocket_rate_from_24k_pcm() -> None:
    pcm_24k = sine_wave_pcm16(config.GEMINI_OUTPUT_SAMPLE_RATE_HZ, freq=440, seconds=0.1)

    transcoder = OutboundTranscoder()
    pcm_out = transcoder.transcode(pcm_24k)

    expected_samples = (
        (len(pcm_24k) // 2) * config.AUDIOSOCKET_SAMPLE_RATE_HZ // config.GEMINI_OUTPUT_SAMPLE_RATE_HZ
    )
    assert abs(len(pcm_out) // 2 - expected_samples) <= 2


def test_transcoders_maintain_state_across_chunks_without_error() -> None:
    pcm_in = sine_wave_pcm16(config.GEMINI_OUTPUT_SAMPLE_RATE_HZ, freq=440, seconds=0.5)

    transcoder = OutboundTranscoder()
    chunk_size = 960  # 20ms @ 24kHz PCM16
    total_out = 0
    for i in range(0, len(pcm_in), chunk_size):
        chunk = pcm_in[i : i + chunk_size]
        out = transcoder.transcode(chunk)
        total_out += len(out)
    assert total_out > 0
