"""CallManager keeps every call, even one cut off by a crash: calls in progress are checkpointed to disk and
recovered as "interrupted" on the next start."""
from __future__ import annotations

import json
import wave
from pathlib import Path

import pytest

from audio.recorder import CallRecorder
from data.call_manager import CallManager


def manager(tmp_path: Path) -> CallManager:
    return CallManager(calls_path=tmp_path / "calls.json", recordings_dir=tmp_path / "rec",
                       active_path=tmp_path / "active.json")


@pytest.mark.asyncio
async def test_calls_in_progress_are_checkpointed_as_they_go(tmp_path):
    m = manager(tmp_path)
    await m.call_started("c1", "0771234567")
    await m.record_action("c1", "Caller: I want a manicure", {"speaker": "caller"})
    await m.update_customer("c1", {"name": "Nimali", "service": "Gel Manicure"})

    [saved] = json.loads((tmp_path / "active.json").read_text())
    assert saved["call_id"] == "c1" and saved["customer"]["name"] == "Nimali"
    assert saved["actions"][-1]["action"] == "Caller: I want a manicure"

    await m.call_ended("c1", status="dropped", end_reason="error", error="1011", reconnects=3)
    assert json.loads((tmp_path / "active.json").read_text()) == []
    [record] = await m.get_call_history()
    assert (record["status"], record["end_reason"], record["error"], record["reconnects"]) == \
        ("dropped", "error", "1011", 3)
    assert record["customer"] == {"name": "Nimali", "service": "Gel Manicure"}


@pytest.mark.asyncio
async def test_a_crash_mid_call_is_recovered_with_transcript_customer_and_recording(tmp_path):
    before = manager(tmp_path)
    await before.call_started("c2", "0771234567")
    await before.record_action("c2", "AI Receptionist: Which day suits you?", {"speaker": "ai"})
    await before.update_customer("c2", {"service": "Pedicure"})
    rec = CallRecorder(8000, clock=lambda: 0.0, spool=tmp_path / "rec" / "c2")
    rec.add_caller(b"\x01\x00" * 4000)
    rec._caller.flush()  # what reached the disk before the process died
    rec._agent.flush()

    after = manager(tmp_path)  # the server restarts
    [recovered] = after.recover_interrupted(8000)
    assert recovered.status == "interrupted" and recovered.end_reason == "server_stopped"
    assert recovered.customer == {"service": "Pedicure"}
    assert recovered.recording_file == "c2.wav"
    with wave.open(str(tmp_path / "rec" / "c2.wav"), "rb") as wav:
        assert wav.getnframes() == 4000
    assert not (tmp_path / "active.json").exists()
    assert not list((tmp_path / "rec").glob("*.pcm"))

    [record] = await after.get_call_history()
    assert any(a["action"] == "AI Receptionist: Which day suits you?" for a in record["actions"])
    assert CallManager(calls_path=tmp_path / "calls.json").recording_path("c2") is None  # no recordings dir
    assert after.recording_path("c2") == tmp_path / "rec" / "c2.wav"
    assert after.recover_interrupted(8000) == []  # only once


def test_history_saved_before_these_fields_existed_still_loads(tmp_path):
    (tmp_path / "calls.json").write_text(json.dumps([{
        "call_id": "old", "caller_number": "077", "started_at": "2026-01-01T00:00:00+00:00",
        "ended_at": None, "duration_seconds": 5, "status": "completed", "actions": [], "recording_file": None,
    }]))
    m = CallManager(calls_path=tmp_path / "calls.json")
    assert m._completed_calls[0].customer == {} and m._completed_calls[0].reconnects == 0
