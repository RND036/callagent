from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class SalonSettings:
    salon_name: str = "CLASSY NAIL SPA"
    salon_address: str = "309 Kotte Road, Nugegoda 10250, Sri Lanka"
    salon_phone: str = "+94 77 741 2853"
    transfer_phone: str = "+94777412853"
    operating_hours_display: str = "Daily from 9:00 AM to 7:00 PM (open 7 days a week)"
    business_hours_start: str = "09:00"
    business_hours_end: str = "19:00"
    greeting_sinhala: str = "ආයුබෝවන්! {salon_name} වෙත සාදරයෙන් පිළිගනිමු. මම ඔබට කොහොමද උදව් කරන්නේ?"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class SettingsStore:
    """Thread-safe persistent store for salon and AI voice agent settings."""

    def __init__(self, settings_path: Path | None = None) -> None:
        self._path = settings_path
        self._lock = asyncio.Lock()
        self._settings = SalonSettings()

        if self._path and self._path.exists():
            try:
                data = json.loads(self._path.read_text())
                if isinstance(data, dict):
                    # Filter only fields that exist on SalonSettings
                    valid_keys = set(SalonSettings.__dataclass_fields__.keys())
                    filtered = {k: v for k, v in data.items() if k in valid_keys}
                    self._settings = SalonSettings(**filtered)
            except Exception as e:
                logger.warning("Failed loading settings from %s, using defaults: %s", self._path, e)
        elif self._path:
            self._persist()

    def _persist(self) -> None:
        if not self._path:
            return
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._path.write_text(json.dumps(self._settings.to_dict(), indent=2))
        except Exception as e:
            logger.error("Failed persisting settings to %s: %s", self._path, e)

    async def get_settings(self) -> SalonSettings:
        async with self._lock:
            return SalonSettings(**self._settings.to_dict())

    async def update_settings(self, updates: dict[str, Any]) -> SalonSettings:
        async with self._lock:
            current = self._settings.to_dict()
            valid_keys = set(SalonSettings.__dataclass_fields__.keys())
            for k, v in updates.items():
                if k in valid_keys and v is not None:
                    current[k] = str(v).strip() if isinstance(v, str) else v
            self._settings = SalonSettings(**current)
            self._persist()
            return SalonSettings(**self._settings.to_dict())
