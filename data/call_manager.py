from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


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
    status: str = "ringing"  # ringing, in-call, completed, transferred, ended, failed
    actions: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class CallManager:
    """Tracks active calls, records event history, and broadcasts real-time updates."""

    def __init__(self, calls_path: Path | None = None) -> None:
        self._calls_path = calls_path
        self._active_calls: dict[str, CallRecord] = {}
        self._completed_calls: list[CallRecord] = []
        self._subscribers: set[asyncio.Queue] = set()
        self._lock = asyncio.Lock()

        if self._calls_path and self._calls_path.exists():
            try:
                raw = json.loads(self._calls_path.read_text())
                self._completed_calls = [
                    CallRecord(**item) for item in raw if isinstance(item, dict)
                ]
            except Exception as e:
                logger.warning("Failed to load calls history from %s: %s", self._calls_path, e)

    def _persist(self) -> None:
        if not self._calls_path:
            return
        try:
            # Keep up to 200 completed calls
            data = [c.to_dict() for c in self._completed_calls[-200:]]
            self._calls_path.write_text(json.dumps(data, indent=2))
        except Exception as e:
            logger.warning("Failed to persist calls to %s: %s", self._calls_path, e)

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

        if record:
            await self.broadcast(
                "call_action",
                {"call_id": call_id, "action": action_obj, "call": record.to_dict()},
            )

    async def call_ended(
        self, call_id: str, status: str = "completed"
    ) -> CallRecord | None:
        now = datetime.now(timezone.utc)
        record: CallRecord | None = None
        async with self._lock:
            record = self._active_calls.pop(call_id, None)
            if record:
                record.ended_at = now.isoformat()
                record.status = status
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

        if record:
            await self.broadcast("call_ended", record.to_dict())
            logger.info(
                "Call ended registered: %s (status: %s, duration: %ss)",
                call_id,
                status,
                record.duration_seconds,
            )
        return record

    async def get_active_calls(self) -> list[dict[str, Any]]:
        async with self._lock:
            return [c.to_dict() for c in self._active_calls.values()]

    async def get_call_history(self, limit: int = 50) -> list[dict[str, Any]]:
        async with self._lock:
            return [c.to_dict() for c in self._completed_calls[:limit]]
