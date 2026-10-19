import struct

import config
from audio.noise_gate import NearFieldGate, build_caller_gate

_RATE = 8000
_FRAME_SAMPLES = 160  # 20ms @ 8kHz


def frame(level: int, samples: int = _FRAME_SAMPLES) -> bytes:
    """Square wave alternating +level/-level, so its RMS is exactly `level`."""
    return struct.pack(f"<{samples}h", *([level, -level] * (samples // 2)))


def make_gate(**overrides) -> NearFieldGate:
    params = dict(open_ratio=3.0, min_open_rms=400, attack_ms=40, hangover_ms=100, preroll_ms=100)
    params.update(overrides)
    return NearFieldGate(_RATE, **params)


def silence_like(pcm: bytes) -> bytes:
    return bytes(len(pcm))


def test_quiet_background_is_replaced_with_same_length_silence() -> None:
    gate = make_gate()
    for _ in range(20):
        chunk = frame(150)
        assert gate.process(chunk) == [silence_like(chunk)]
    assert not gate.is_open


def test_near_speech_opens_gate_and_replays_preroll() -> None:
    gate = make_gate()
    for _ in range(20):
        gate.process(frame(100))

    first = frame(2000)
    assert gate.process(first) == [silence_like(first)]  # still in the 40ms attack

    second = frame(2000)
    out = gate.process(second)
    assert gate.is_open
    # Pre-roll replays 100ms (3 lead-in + 2 attack chunks) so the first word isn't clipped.
    assert out == [frame(100)] * 3 + [first, second]

    third = frame(2000)
    assert gate.process(third) == [third]


def test_single_click_does_not_open_gate() -> None:
    gate = make_gate()
    for _ in range(20):
        gate.process(frame(100))
    click = frame(5000)
    assert gate.process(click) == [silence_like(click)]
    quiet = frame(100)
    assert gate.process(quiet) == [silence_like(quiet)]
    assert not gate.is_open


def test_hangover_keeps_word_endings_then_closes() -> None:
    gate = make_gate(hangover_ms=100)
    for _ in range(20):
        gate.process(frame(100))
    for _ in range(5):
        gate.process(frame(2000))
    assert gate.is_open

    tails = [frame(300) for _ in range(4)]  # 80ms of quieter audio: within hangover
    for tail in tails:
        assert gate.process(tail) == [tail]
    assert gate.is_open

    last = frame(300)
    assert gate.process(last) == [silence_like(last)]  # 100ms quiet: closes
    assert not gate.is_open


def test_steady_loud_background_raises_threshold_but_near_voice_still_passes() -> None:
    gate = make_gate()
    for _ in range(150):  # 3s of a TV at a level above min_open_rms
        gate.process(frame(600))
    assert gate.noise_floor == 600

    tv_peak = frame(1200)  # louder bit of the TV, but under 3x the background
    for _ in range(5):
        assert gate.process(tv_peak) == [silence_like(tv_peak)]
    assert not gate.is_open

    for _ in range(3):
        gate.process(frame(3000))
    assert gate.is_open


def test_distant_voice_below_min_level_never_opens_in_quiet_room() -> None:
    gate = make_gate(min_open_rms=400)
    for _ in range(50):
        gate.process(frame(20))
    distant = frame(350)  # >3x a near-silent room, but under the absolute minimum
    for _ in range(10):
        assert gate.process(distant) == [silence_like(distant)]
    assert not gate.is_open


def test_summary_reports_levels() -> None:
    gate = make_gate()
    for _ in range(10):
        gate.process(frame(100))
    for _ in range(10):
        gate.process(frame(2000))
    summary = gate.summary()
    assert "loudest 2000 RMS" in summary
    assert "passed" in summary


def test_build_caller_gate_respects_enabled_flag(monkeypatch) -> None:
    monkeypatch.setattr(config, "NOISE_GATE_ENABLED", False)
    assert build_caller_gate() is None
    monkeypatch.setattr(config, "NOISE_GATE_ENABLED", True)
    assert isinstance(build_caller_gate(), NearFieldGate)
