import wave

from audio.recorder import CallRecorder


class FakeClock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


def test_tracks_are_aligned_to_the_clock_and_gaps_padded(tmp_path) -> None:
    clock = FakeClock()
    rec = CallRecorder(8000, clock=clock)

    rec.add_caller(b"\x01\x00" * 800)  # 0.1s of caller at t=0
    clock.now += 0.5
    rec.add_agent(b"\x02\x00" * 800)  # agent starts 0.5s in -> 0.5s of silence before it

    path = tmp_path / "out" / "call.wav"
    rec.write_wav(path)

    with wave.open(str(path), "rb") as wav:
        assert (wav.getnchannels(), wav.getsampwidth(), wav.getframerate()) == (1, 2, 8000)
        frames = wav.readframes(wav.getnframes())
    samples = [int.from_bytes(frames[i : i + 2], "little", signed=True) for i in range(0, len(frames), 2)]

    assert len(samples) == 4000 + 800  # 0.5s offset + 0.1s of agent audio
    assert samples[0] == 1 and samples[799] == 1  # caller at the start
    assert samples[800] == 0 and samples[3999] == 0  # padded gap
    assert samples[4000] == 2  # agent at its send-time offset


def test_overlapping_speech_is_mixed_and_clipped(tmp_path) -> None:
    rec = CallRecorder(8000, clock=FakeClock())
    loud = (30000).to_bytes(2, "little", signed=True) * 10
    rec.add_caller(loud)
    rec.add_agent(loud)
    rec.write_wav(tmp_path / "c.wav")
    with wave.open(str(tmp_path / "c.wav"), "rb") as wav:
        frames = wav.readframes(wav.getnframes())
    assert int.from_bytes(frames[:2], "little", signed=True) == 32767


def test_has_audio() -> None:
    rec = CallRecorder(8000, clock=FakeClock())
    assert not rec.has_audio()
    rec.add_caller(b"\x00\x00")
    assert rec.has_audio()


def _spool_files(tmp_path):
    return sorted(p.name for p in tmp_path.glob("*.pcm"))


def test_spooled_tracks_are_on_disk_during_the_call_and_removed_after(tmp_path) -> None:
    clock = FakeClock()
    rec = CallRecorder(8000, clock=clock, spool=tmp_path / "call-1")
    rec.add_caller(b"\x01\x00" * 12000)  # 1.5 s: more than the ~1 s write buffer, so it reaches the disk
    assert _spool_files(tmp_path) == ["call-1.agent.pcm", "call-1.caller.pcm"]
    assert (tmp_path / "call-1.caller.pcm").stat().st_size > 0
    clock.now += 1.5
    rec.add_agent(b"\x02\x00" * 800)
    assert rec.has_audio()

    rec.write_wav(tmp_path / "call-1.wav")
    assert _spool_files(tmp_path) == []
    with wave.open(str(tmp_path / "call-1.wav"), "rb") as wav:
        assert wav.getnframes() == 12000 + 800  # agent at 1.5 s


def test_finish_spool_recovers_a_call_that_never_finished(tmp_path) -> None:
    from audio.recorder import finish_spool

    rec = CallRecorder(8000, clock=FakeClock(), spool=tmp_path / "call-2")
    rec.add_caller(b"\x03\x00" * 8000)
    rec._caller.flush()  # what the OS has when the process dies
    rec._agent.flush()

    assert finish_spool(tmp_path / "call-2", 8000, tmp_path / "call-2.wav")
    assert _spool_files(tmp_path) == []
    with wave.open(str(tmp_path / "call-2.wav"), "rb") as wav:
        assert wav.getnframes() == 8000
    assert not finish_spool(tmp_path / "missing", 8000, tmp_path / "missing.wav")


def test_discard_removes_the_spool(tmp_path) -> None:
    rec = CallRecorder(8000, clock=FakeClock(), spool=tmp_path / "call-3")
    assert not rec.has_audio()
    rec.discard()
    assert _spool_files(tmp_path) == []
