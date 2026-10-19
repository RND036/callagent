from __future__ import annotations

import io
import math
import struct
import wave

import pytest

from audio.prompt_audio import to_line_pcm
from data.ivr_store import IvrStore, validate_ivr_config


def make_wav(rate=44100, channels=2, seconds=0.5, width=2, amplitude=8000) -> bytes:
    frames = bytearray()
    for i in range(int(rate * seconds)):
        sample = int(amplitude * math.sin(2 * math.pi * 440 * i / rate))
        for _ in range(channels):
            frames += struct.pack("<h", sample)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(width)
        w.setframerate(rate)
        w.writeframes(bytes(frames))
    return buf.getvalue()


def test_validation_rejects_bad_values() -> None:
    with pytest.raises(ValueError):
        validate_ivr_config({"options": [{"digit": "1", "language": "si"}, {"digit": "1", "language": "en"}]})
    with pytest.raises(ValueError):
        validate_ivr_config({"options": [{"digit": "x", "language": "si"}]})
    with pytest.raises(ValueError):
        validate_ivr_config({"options": [{"digit": "1", "language": "fr"}]})
    with pytest.raises(ValueError):
        validate_ivr_config({"options": []})
    with pytest.raises(ValueError):
        validate_ivr_config({"max_tries": 9})
    with pytest.raises(ValueError):
        validate_ivr_config({"wait_seconds": 1})
    with pytest.raises(ValueError):
        validate_ivr_config({"default_language": "xx"})


@pytest.mark.asyncio
async def test_config_and_prompt_survive_reload(tmp_path) -> None:
    store = IvrStore(tmp_path / "ivr.json", tmp_path / "prompts")
    assert not store.is_active()
    await store.update({"enabled": True, "max_tries": 2, "options": [{"digit": "5", "language": "ta"}]})
    assert not store.is_active()  # enabled, but no recording yet
    await store.save_prompt("menu", make_wav(), "menu.wav")
    assert store.is_active()

    reloaded = IvrStore(tmp_path / "ivr.json", tmp_path / "prompts")
    state = await reloaded.get()
    assert state["max_tries"] == 2 and state["options"] == [{"digit": "5", "language": "ta"}]
    assert state["prompts"]["menu"]["original_name"] == "menu.wav"
    assert reloaded.is_active()


@pytest.mark.asyncio
async def test_upload_is_converted_to_8k_mono(tmp_path) -> None:
    store = IvrStore(tmp_path / "ivr.json", tmp_path / "prompts")
    meta = await store.save_prompt("menu", make_wav(rate=44100, channels=2, seconds=0.5), "x.wav")
    assert meta["duration_s"] == 0.5
    with wave.open(str(store.prompt_path("menu")), "rb") as w:
        assert (w.getframerate(), w.getnchannels(), w.getsampwidth()) == (8000, 1, 2)
    assert len(store.load_prompt_pcm("menu", rate=8000)) == 8000  # 0.5 s of 16-bit samples
    assert abs(len(store.load_prompt_pcm("menu", rate=16000)) - 16000) <= 8  # resampler rounding


@pytest.mark.asyncio
async def test_bad_uploads_and_unknown_slot(tmp_path) -> None:
    store = IvrStore(tmp_path / "ivr.json", tmp_path / "prompts")
    with pytest.raises(ValueError):
        await store.save_prompt("menu", b"", "empty.wav")
    with pytest.raises(ValueError):
        await store.save_prompt("menu", make_wav(amplitude=0), "silent.wav")
    with pytest.raises(ValueError):
        await store.save_prompt("menu", make_wav(rate=8000, channels=1, seconds=61), "long.wav")
    with pytest.raises(ValueError):
        await store.save_prompt("menu", b"this is not audio", "junk.mp3")
    with pytest.raises(KeyError):
        await store.save_prompt("../evil", make_wav(), "x.wav")
    assert store.prompt_path("../evil") is None


@pytest.mark.asyncio
async def test_delete_prompt(tmp_path) -> None:
    store = IvrStore(tmp_path / "ivr.json", tmp_path / "prompts")
    await store.save_prompt("invalid", make_wav(), "bad.wav")
    assert store.prompt_path("invalid").exists()
    assert await store.delete_prompt("invalid") is True
    assert not store.prompt_path("invalid").exists()
    assert store.load_prompt_pcm("invalid") is None
    assert await store.delete_prompt("invalid") is False


def test_mono_wav_passes_through_at_line_rate() -> None:
    assert len(to_line_pcm(make_wav(rate=8000, channels=1, seconds=0.25, width=2))) == 4000


@pytest.mark.asyncio
async def test_menu_recording_is_copied_for_asterisk(tmp_path, monkeypatch) -> None:
    import config

    target = tmp_path / "sounds" / "lang-menu.wav"
    target.parent.mkdir()
    monkeypatch.setattr(config, "IVR_ASTERISK_MENU_PATH", str(target))
    store = IvrStore(tmp_path / "ivr.json", tmp_path / "prompts")
    await store.save_prompt("invalid", make_wav(), "bad.wav")
    assert not target.exists()  # only the menu recording is shared
    await store.save_prompt("menu", make_wav(), "menu.wav")
    with wave.open(str(target), "rb") as w:
        assert (w.getframerate(), w.getnchannels()) == (8000, 1)
    await store.delete_prompt("menu")
    assert not target.exists()
