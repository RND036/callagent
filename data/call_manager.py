from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
import uuid as uuid_module
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from audio.recorder import finish_spool

logger = logging.getLogger(__name__)

# The dialplan registers a call's caller number and IVR language right before
# AudioSocket() connects; anything not claimed by a call within this long is
# stale.
_PENDING_CALL_TTL_SECONDS = 120.0

_MAX_COMPLETED_CALLS = 200


@dataclass(frozen=True)
class PendingCall:
    caller_number: str | None
    language: str | None  # None: the dialplan did not ask -- the agent's own language menu decides
    registered_at: float


@dataclass
class CallAction:
    timestamp: str
    action: str
    details: dict[str, Any] = field(default_factory=dict)


@dataclass
class CallRecord:
    call_id: str
    caller_number: str
    started_at: str
    ended_at: str | None = None
    duration_seconds: int | None = None
    # ringing, in-call, completed, transferred, failed; dropped = the AI could not carry on (Gemini stayed
    # unreachable), interrupted = the server stopped in the middle of the call
    status: str = "ringing"
    actions: list[dict[str, Any]] = field(default_factory=list)
    recording_file: str | None = None  # file name inside the recordings dir
    end_reason: str | None = None  # how the agent's part ended: completed, caller_hung_up, time_limit, error...
    error: str | None = None
    reconnects: int = 0  # Live reconnections during the call
    customer: dict[str, str] = field(default_factory=dict)  # what the call established (tools.context.CallContext)
    # Asterisk's RTCP view of the caller's line, from the dialplan hangup handler (/api/pbx/call-end): packets
    # sent/received, how many the far end lost, jitter, round-trip time. Empty when it never arrived.
    line_quality: dict[str, float] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class CallManager:
    """Tracks active calls, records event history, and broadcasts real-time updates."""

    def __init__(
        self, calls_path: Path | None = None, recordings_dir: Path | None = None, active_path: Path | None = None
    ) -> None:
        self._calls_path = calls_path
        self._recordings_dir = recordings_dir
        self._active_path = active_path  # calls in progress, rewritten as they go on (see recover_interrupted)
        self._active_calls: dict[str, CallRecord] = {}
        self._completed_calls: list[CallRecord] = []
        self._subscribers: set[asyncio.Queue] = set()
        self._lock = asyncio.Lock()
        self._pending_calls: dict[str, PendingCall] = {}  # call UUID -> details from the dialplan
        self._early_quality: dict[str, dict[str, float]] = {}  # line stats that arrived before the call ended

        if self._calls_path and self._calls_path.exists():
            try:
                raw = json.loads(self._calls_path.read_text())
                self._completed_calls = [
                    CallRecord(**item) for item in raw if isinstance(item, dict)
                ]
            except Exception as e:
                logger.warning("Failed to load calls history from %s: %s", self._calls_path, e)

    @property
    def recordings_dir(self) -> Path | None:
        return self._recordings_dir

    def recording_path(self, call_id: str) -> Path | None:
        """The recording file for a finished call, only ever resolved through
        the stored record (never from caller-supplied path text)."""
        if self._recordings_dir is None:
            return None
        for call in self._completed_calls:
            if call.call_id == call_id and call.recording_file:
                path = self._recordings_dir / Path(call.recording_file).name
                return path if path.is_file() else None
        return None

    def _prune_old_calls(self) -> None:
        """Keep the newest calls (the list is newest-first) and delete the
        recordings of the ones dropped."""
        dropped = self._completed_calls[_MAX_COMPLETED_CALLS:]
        del self._completed_calls[_MAX_COMPLETED_CALLS:]
        if self._recordings_dir is None:
            return
        for call in dropped:
            if call.recording_file:
                try:
                    (self._recordings_dir / Path(call.recording_file).name).unlink(missing_ok=True)
                except OSError as e:
                    logger.warning("Failed to delete old recording %s: %s", call.recording_file, e)

    def _persist(self) -> None:
        if not self._calls_path:
            return
        try:
            self._prune_old_calls()
            data = [c.to_dict() for c in self._completed_calls]
            _write_atomically(self._calls_path, json.dumps(data, indent=2))
        except Exception as e:
            logger.warning("Failed to persist calls to %s: %s", self._calls_path, e)

    def _checkpoint(self) -> None:
        """Save the calls in progress, so a crash mid-call still leaves their transcript and customer details."""
        if not self._active_path:
            return
        try:
            data = [c.to_dict() for c in self._active_calls.values()]
            _write_atomically(self._active_path, json.dumps(data))
        except Exception as e:
            logger.warning("Failed to save calls in progress to %s: %s", self._active_path, e)

    def recover_interrupted(self, recording_rate: int | None = None) -> list[CallRecord]:
        """Calls still in progress when the server last stopped (crash/restart): moves them to the history as
        "interrupted", with their transcript so far and the audio that reached the disk. Call once at startup."""
        if not self._active_path or not self._active_path.exists():
            return []
        try:
            raw = json.loads(self._active_path.read_text())
        except Exception as e:
            logger.warning("Failed to read calls in progress from %s: %s", self._active_path, e)
            raw = []
        recovered: list[CallRecord] = []
        for item in raw if isinstance(raw, list) else []:
            try:
                record = CallRecord(**item)
            except TypeError:
                continue
            record.ended_at = record.actions[-1]["timestamp"] if record.actions else record.started_at
            record.status = "interrupted"
            record.end_reason = record.end_reason or "server_stopped"
            try:
                record.duration_seconds = max(0, int((datetime.fromisoformat(record.ended_at)
                                                      - datetime.fromisoformat(record.started_at)).total_seconds()))
            except Exception:
                record.duration_seconds = 0
            if self._recordings_dir is not None and recording_rate:
                name = f"{Path(record.call_id).name}.wav"
                try:
                    if finish_spool(self._recordings_dir / Path(record.call_id).name, recording_rate,
                                    self._recordings_dir / name):
                        record.recording_file = name
                except Exception:  # noqa: BLE001 - keep the rest of the record
                    logger.exception("Failed to recover the recording of call %s", record.call_id)
            record.actions.append({"timestamp": record.ended_at, "action": "Call Ended (interrupted)",
                                   "details": {"duration_seconds": record.duration_seconds}})
            self._completed_calls.insert(0, record)
            recovered.append(record)
            logger.warning("Recovered call %s interrupted by a server stop (caller: %s)", record.call_id,
                           record.caller_number)
        if recovered:
            self._persist()
        self._active_path.unlink(missing_ok=True)
        return recovered

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue()
        self._subscribers.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self._subscribers.discard(q)

    async def broadcast(self, event_type: str, data: dict[str, Any]) -> None:
        payload = {
            "type": event_type,
            "data": data,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        for q in list(self._subscribers):
            try:
                q.put_nowait(payload)
            except Exception:
                self._subscribers.discard(q)

    def register_pending_call(self, call_id: str, caller_number: str | None, language: str | None) -> None:
        """Remember the caller's number and IVR language for a call that's
        about to connect (AudioSocket itself only carries the call UUID)."""
        now = time.monotonic()
        self._pending_calls = {
            cid: entry
            for cid, entry in self._pending_calls.items()
            if now - entry.registered_at < _PENDING_CALL_TTL_SECONDS
        }
        self._pending_calls[_normalize_call_id(call_id)] = PendingCall(caller_number, language, now)

    def take_pending_call(self, call_id: str | None) -> PendingCall | None:
        if not call_id:
            return None
        entry = self._pending_calls.pop(_normalize_call_id(call_id), None)
        if entry is None or time.monotonic() - entry.registered_at >= _PENDING_CALL_TTL_SECONDS:
            return None
        return entry

    async def call_started(self, call_id: str, caller_number: str) -> CallRecord:
        now_iso = datetime.now(timezone.utc).isoformat()
        record = CallRecord(
            call_id=call_id,
            caller_number=caller_number or "Unknown",
            started_at=now_iso,
            status="in-call",
            actions=[{"timestamp": now_iso, "action": "Call Connected", "details": {}}],
        )
        async with self._lock:
            self._active_calls[call_id] = record
            self._checkpoint()

        await self.broadcast("call_started", record.to_dict())
        logger.info("Call started registered: %s (caller: %s)", call_id, caller_number)
        return record

    async def record_action(
        self, call_id: str, action: str, details: dict[str, Any] | None = None
    ) -> None:
        now_iso = datetime.now(timezone.utc).isoformat()
        action_obj = {"timestamp": now_iso, "action": action, "details": details or {}}
        record: CallRecord | None = None
        async with self._lock:
            record = self._active_calls.get(call_id)
            if record:
                record.actions.append(action_obj)
                self._checkpoint()

        if record:
            await self.broadcast(
                "call_action",
                {"call_id": call_id, "action": action_obj, "call": record.to_dict()},
            )

    async def update_customer(self, call_id: str, customer: dict[str, str]) -> None:
        """What the call has established about the caller so far (name, service, booking...)."""
        async with self._lock:
            record = self._active_calls.get(call_id)
            if record:
                record.customer = dict(customer)
                self._checkpoint()
        if record:
            await self.broadcast("call_updated", record.to_dict())

    async def call_ended(
        self,
        call_id: str,
        status: str = "completed",
        recording_file: str | None = None,
        end_reason: str | None = None,
        error: str | None = None,
        reconnects: int = 0,
        customer: dict[str, str] | None = None,
    ) -> CallRecord | None:
        now = datetime.now(timezone.utc)
        record: CallRecord | None = None
        async with self._lock:
            record = self._active_calls.pop(call_id, None)
            if record:
                record.ended_at = now.isoformat()
                record.status = status
                record.recording_file = recording_file
                record.end_reason = end_reason
                record.error = error
                record.reconnects = reconnects
                if early := self._early_quality.pop(call_id, None):
                    record.line_quality = early
                if customer:
                    record.customer = dict(customer)
                try:
                    start_dt = datetime.fromisoformat(record.started_at)
                    record.duration_seconds = max(0, int((now - start_dt).total_seconds()))
                except Exception:
                    record.duration_seconds = 0
                record.actions.append(
                    {
                        "timestamp": now.isoformat(),
                        "action": f"Call Ended ({status})",
                        "details": {"duration_seconds": record.duration_seconds},
                    }
                )
                self._completed_calls.insert(0, record)
                self._persist()
                self._checkpoint()

        if record:
            await self.broadcast("call_ended", record.to_dict())
            logger.info(
                "Call ended registered: %s (status: %s, duration: %ss)",
                call_id,
                status,
                record.duration_seconds,
            )
        return record

    async def set_line_quality(self, call_id: str, qos: dict[str, float]) -> None:
        """Store the line stats for a call (the dialplan reports them as the channel hangs up, which can be just
        before or just after the agent finishes the call)."""
        call_id = _normalize_call_id(call_id)
        record: CallRecord | None = None
        async with self._lock:
            record = self._active_calls.get(call_id)
            if record is not None:
                record.line_quality = dict(qos)
                self._checkpoint()
            else:
                record = next((c for c in self._completed_calls if c.call_id == call_id), None)
                if record is not None:
                    record.line_quality = dict(qos)
                    self._persist()
                else:
                    self._early_quality[call_id] = dict(qos)
        logger.info("Line quality for call %s: %s", call_id, describe_line_quality(qos))
        if record is not None:
            await self.broadcast("call_updated", record.to_dict())

    async def get_active_calls(self) -> list[dict[str, Any]]:
        async with self._lock:
            return [c.to_dict() for c in self._active_calls.values()]

    async def get_call(self, call_id: str) -> dict[str, Any] | None:
        async with self._lock:
            for call in list(self._active_calls.values()) + self._completed_calls:
                if call.call_id == call_id:
                    return call.to_dict()
        return None

    async def get_call_history(self, limit: int = 50) -> list[dict[str, Any]]:
        async with self._lock:
            return [c.to_dict() for c in self._completed_calls[:limit]]


class TranscriptLog:
    """Turns the live transcription fragments of one call into whole
    utterances on the call record: consecutive fragments from the same speaker
    are merged and written as a single "Caller: ..." / "AI Receptionist: ..."
    action once the speaker changes (or flush() is called at a turn boundary)."""

    _LABELS = {"caller": "Caller", "ai": "AI Receptionist"}

    def __init__(self, call_manager: CallManager | None, call_id: str | None) -> None:
        self._call_manager = call_manager
        self._call_id = call_id
        self._speaker: str | None = None
        self._buffer = ""
        self.turns: list[dict] = []  # finished turns (role caller | ai | tool), for call_recap after a reconnect

    def turns_so_far(self) -> int:
        """How many turns there are once the one in progress is finished."""
        return len(self.turns) + (1 if self._speaker and self._buffer.strip() else 0)

    def pending(self, speaker: str) -> str:
        """What `speaker` has said so far in the current, unfinished turn."""
        return self._buffer.strip() if speaker == self._speaker else ""

    async def note(self, role: str, text: str) -> None:
        """A non-speech line (e.g. a tool that ran), kept for the recap."""
        await self.flush()
        self.turns.append({"role": role, "text": text})

    async def add(self, speaker: str, text: str | None) -> None:
        text = _PLACEHOLDER_RE.sub("", text or "")
        if not text:
            return
        if speaker != self._speaker:
            await self.flush()
            self._speaker = speaker
        self._buffer += text

    async def flush(self) -> None:
        speaker, text = self._speaker, self._buffer.strip()
        self._speaker, self._buffer = None, ""
        if text and speaker:
            self.turns.append({"role": speaker, "text": text})
        if text and speaker and self._call_manager and self._call_id:
            await self._call_manager.record_action(
                self._call_id, f"{self._LABELS[speaker]}: {text}", {"speaker": speaker, "text": text}
            )


# Gemini's transcription of a silent stretch of the agent's own audio; not something that was said.
_PLACEHOLDER_RE = re.compile(r"<\s*no[ _]speech\s*>|\{\s*pause\s*\}", re.IGNORECASE)


def parse_qos(raw: str) -> dict[str, float]:
    """Asterisk's RTPAUDIOQOS ("ssrc=..;lp=0;rxjitter=0.002;rxcount=412;txcount=410;rlp=3;rtt=0.18...") as numbers;
    anything that is not a number is dropped."""
    out: dict[str, float] = {}
    for part in (raw or "").split(";"):
        key, _, value = part.partition("=")
        try:
            out[key.strip()] = float(value)
        except ValueError:
            continue
    return out


def lost_to_caller_percent(qos: dict[str, float]) -> float | None:
    """Share of our packets the caller's side reported lost (rlp = lost by the far end, txcount = we sent)."""
    sent = qos.get("txcount", 0.0)
    return round(100 * qos.get("rlp", 0.0) / sent, 1) if sent else None


def describe_line_quality(qos: dict[str, float]) -> str:
    lost = lost_to_caller_percent(qos)
    return (
        f"sent {qos.get('txcount', 0):.0f} packets, far end lost {qos.get('rlp', 0):.0f}"
        + (f" ({lost}%)" if lost is not None else "")
        + f"; received {qos.get('rxcount', 0):.0f}, lost {qos.get('lp', 0):.0f}"
        + f"; jitter rx {qos.get('rxjitter', 0) * 1000:.0f} ms / tx {qos.get('txjitter', 0) * 1000:.0f} ms"
        + f"; rtt {qos.get('rtt', 0) * 1000:.0f} ms"
    )


def _write_atomically(path: Path, text: str) -> None:
    """Never leaves a half-written file behind, even if the process dies mid-write."""
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text)
    os.replace(tmp, path)


def _normalize_call_id(call_id: str) -> str:
    """Canonical lowercase dashed form, so the dialplan's UUID string and the
    one parsed from the AudioSocket packet always match."""
    try:
        return str(uuid_module.UUID(call_id))
    except ValueError:
        return call_id.strip().lower()
