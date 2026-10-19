from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import config
import languages

logger = logging.getLogger(__name__)

# Gemini Live prebuilt voices the dashboard offers.
AI_VOICES = [
    "Aoede", "Kore", "Puck", "Charon", "Fenrir", "Leda", "Orus", "Zephyr", "Callirrhoe", "Autonoe", "Enceladus",
    "Iapetus", "Umbriel", "Algieba", "Despina", "Erinome", "Algenib", "Rasalgethi", "Laomedeia", "Achernar",
    "Alnilam", "Schedar", "Gacrux", "Pulcherrima", "Achird", "Zubenelgenubi", "Vindemiatrix", "Sadachbia",
    "Sadaltager", "Sulafat",
]


def validate_ai_settings(updates: dict[str, Any]) -> dict[str, Any]:
    """Cleaned copy of the validated fields in `updates` (AI voice/temperature/pause, branches, WhatsApp); raises
    ValueError on a bad value."""
    out = dict(updates)
    if "branches" in out:
        names = [n.strip() for n in str(out["branches"]).split(",") if n.strip()]
        if not names:
            raise ValueError("Add at least one branch.")
        if len({n.lower() for n in names}) != len(names):
            raise ValueError("Branch names must be different from each other.")
        out["branches"] = ", ".join(names)
    if "whatsapp_number" in out:
        number = str(out["whatsapp_number"]).strip()
        if number and not all(c.isdigit() or c in "+ -" for c in number):
            raise ValueError("WhatsApp number may only contain digits, spaces, + and -.")
        out["whatsapp_number"] = number
    if "ai_voice" in out:
        voice = str(out["ai_voice"]).strip()
        if voice not in AI_VOICES and voice != config.GEMINI_VOICE_NAME:
            raise ValueError(f"Unknown voice '{voice}'.")
        out["ai_voice"] = voice
    if "ai_temperature" in out:
        raw = str(out["ai_temperature"]).strip()
        if raw:  # blank = the model's default
            try:
                value = float(raw)
            except ValueError:
                raise ValueError("Temperature must be a number between 0 and 2.") from None
            if not 0.0 <= value <= 2.0:
                raise ValueError("Temperature must be between 0 and 2.")
            raw = f"{value:g}"
        out["ai_temperature"] = raw
    if "ai_vad_silence_ms" in out:
        try:
            ms = int(out["ai_vad_silence_ms"])
        except (TypeError, ValueError):
            raise ValueError("Silence wait must be a whole number of milliseconds.") from None
        if not 300 <= ms <= 3000:
            raise ValueError("Silence wait must be between 300 and 3000 ms.")
        out["ai_vad_silence_ms"] = ms
    return out


@dataclass
class SalonSettings:
    salon_name: str = "CLASSY NAIL SPA"
    salon_address: str = "309 Kotte Road, Nugegoda 10250, Sri Lanka"
    # How the voice says the address on Sinhala/Tamil calls (numbers in words, no postal code).
    salon_address_sinhala: str = field(default_factory=lambda: languages.get("si").spoken_address)
    salon_address_tamil: str = field(default_factory=lambda: languages.get("ta").spoken_address)
    salon_phone: str = "+94 77 741 2853"
    transfer_phone: str = "+94777412853"
    operating_hours_display: str = "Daily from 9:00 AM to 7:00 PM (open 7 days a week)"
    business_hours_start: str = "09:00"
    business_hours_end: str = "19:00"
    # The greetings default to the language files (languages/*.yaml).
    greeting_sinhala: str = field(default_factory=lambda: languages.get("si").greeting)
    greeting_tamil: str = field(default_factory=lambda: languages.get("ta").greeting)
    greeting_english: str = field(default_factory=lambda: languages.get("en").greeting)
    branches: str = "Nugegoda, Hokandara"  # comma-separated branch names callers book at (addresses live in the FAQ)
    whatsapp_number: str = ""  # where callers send design photos
    # The AI agent (applies to the next call; a call in progress keeps its settings so the voice never changes mid-call).
    ai_voice: str = config.GEMINI_VOICE_NAME
    ai_temperature: str = "" if config.GEMINI_TEMPERATURE is None else f"{config.GEMINI_TEMPERATURE:g}"  # blank = default
    ai_vad_silence_ms: int = config.VAD_SILENCE_MS  # how long the caller must pause before the agent answers

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @property
    def branch_list(self) -> list[str]:
        return [b.strip() for b in self.branches.split(",") if b.strip()]


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
