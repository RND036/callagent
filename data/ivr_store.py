"""The language menu callers hear before the AI answers: which keys pick which language, plus the recordings
staff upload on the dashboard. Recordings are stored as 8 kHz mono WAV (what the line plays and the browser previews)."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import wave
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import config
from audio.codec import resample_pcm16
from audio.prompt_audio import LINE_RATE_HZ, duration_seconds, pcm_to_wav, to_line_pcm
import languages
from gemini_session.persona import DEFAULT_LANGUAGE, LANGUAGES

logger = logging.getLogger(__name__)

PROMPT_SLOTS = ("menu", "invalid")  # the whitelist: file names are never built from user input
VALID_KEYS = set("0123456789*#")

DEFAULT_CONFIG: dict[str, Any] = {
    "enabled": False,
    "options": [{"digit": lang.menu_digit, "language": code} for code, lang in languages.languages().items()],
    "default_language": DEFAULT_LANGUAGE,
    "max_tries": 3,
    "wait_seconds": 5,
}


def validate_ivr_config(updates: dict[str, Any]) -> dict[str, Any]:
    """Cleaned copy of the menu fields in `updates`; raises ValueError (message for the admin) on a bad value."""
    out: dict[str, Any] = {}
    if "enabled" in updates:
        out["enabled"] = bool(updates["enabled"])
    if "options" in updates:
        raw = updates["options"]
        if not isinstance(raw, list) or not raw:
            raise ValueError("Add at least one key.")
        options, seen = [], set()
        for item in raw:
            digit = str(item.get("digit", "") if isinstance(item, dict) else "").strip()
            language = str(item.get("language", "") if isinstance(item, dict) else "").strip()
            if digit not in VALID_KEYS:
                raise ValueError("Keys must be 0-9, * or #.")
            if digit in seen:
                raise ValueError(f"Key {digit} is used more than once.")
            if language not in LANGUAGES:
                raise ValueError(f"Unknown language '{language}'.")
            seen.add(digit)
            options.append({"digit": digit, "language": language})
        out["options"] = options
    if "default_language" in updates:
        language = str(updates["default_language"]).strip()
        if language not in LANGUAGES:
            raise ValueError(f"Unknown language '{language}'.")
        out["default_language"] = language
    for key, low, high, label in (("max_tries", 1, 5, "Tries"), ("wait_seconds", 2, 15, "Wait time")):
        if key in updates:
            try:
                value = int(updates[key])
            except (TypeError, ValueError):
                raise ValueError(f"{label} must be a whole number.") from None
            if not low <= value <= high:
                raise ValueError(f"{label} must be between {low} and {high}.")
            out[key] = value
    return out


class IvrStore:
    def __init__(self, config_path: Path | None = None, prompts_dir: Path | None = None) -> None:
        self._path = config_path
        self._dir = prompts_dir
        self._lock = asyncio.Lock()
        self._cfg: dict[str, Any] = json.loads(json.dumps(DEFAULT_CONFIG))
        self._prompts: dict[str, dict[str, Any] | None] = {slot: None for slot in PROMPT_SLOTS}
        self._pcm_cache: dict[str, tuple[float, int, bytes]] = {}
        if self._path and self._path.exists():
            try:
                data = json.loads(self._path.read_text())
                if isinstance(data, dict):
                    prompts = data.pop("prompts", None) or {}
                    self._cfg.update(validate_ivr_config(data))
                    for slot in PROMPT_SLOTS:
                        meta = prompts.get(slot)
                        if isinstance(meta, dict) and self.prompt_path(slot) and self.prompt_path(slot).exists():
                            self._prompts[slot] = meta
            except Exception as e:  # noqa: BLE001
                logger.warning("Failed loading the language menu from %s, using defaults: %s", self._path, e)
        elif self._path:
            self._persist()

    # -- persistence -------------------------------------------------------------------------------------------
    def _snapshot(self) -> dict[str, Any]:
        return {**json.loads(json.dumps(self._cfg)), "prompts": {s: (dict(m) if m else None) for s, m in self._prompts.items()}}

    def _persist(self) -> None:
        if not self._path:
            return
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self._snapshot(), indent=2, ensure_ascii=False))
            os.replace(tmp, self._path)
        except Exception as e:  # noqa: BLE001
            logger.error("Failed persisting the language menu to %s: %s", self._path, e)

    def prompt_path(self, slot: str) -> Path | None:
        if slot not in PROMPT_SLOTS or self._dir is None:
            return None
        return self._dir / f"{slot}.wav"

    # -- config ------------------------------------------------------------------------------------------------
    async def get(self) -> dict[str, Any]:
        async with self._lock:
            return self._snapshot()

    async def update(self, updates: dict[str, Any]) -> dict[str, Any]:
        cleaned = validate_ivr_config(updates)
        async with self._lock:
            self._cfg.update(cleaned)
            self._persist()
            return self._snapshot()

    def is_active(self) -> bool:
        """Enabled and has a menu recording to play."""
        return bool(self._cfg["enabled"] and self._prompts["menu"])

    def settings(self) -> dict[str, Any]:
        """The current menu settings (no prompts), for the call in progress."""
        return json.loads(json.dumps(self._cfg))

    # -- recordings --------------------------------------------------------------------------------------------
    async def save_prompt(self, slot: str, data: bytes, filename: str = "") -> dict[str, Any]:
        """Convert an upload to line audio and store it. Raises KeyError for an unknown slot, ValueError for an
        unusable file."""
        path = self.prompt_path(slot)
        if path is None:
            raise KeyError(slot)
        pcm = await asyncio.to_thread(to_line_pcm, data)
        wav = pcm_to_wav(pcm)
        meta = {
            "original_name": Path(filename or "recording").name[:120],
            "duration_s": duration_seconds(pcm),
            "uploaded_at": datetime.now(timezone.utc).isoformat(),
        }
        async with self._lock:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            tmp.write_bytes(wav)
            os.replace(tmp, path)
            self._prompts[slot] = meta
            self._pcm_cache.pop(slot, None)
            self._persist()
            self._sync_asterisk_copy(slot, wav)
        return dict(meta)

    @staticmethod
    def _sync_asterisk_copy(slot: str, wav: bytes | None) -> None:
        """Keep Asterisk's copy of the menu recording (used by its own dialplan menu) in step with the upload."""
        target = config.IVR_ASTERISK_MENU_PATH
        if slot != "menu" or not target:
            return
        try:
            path = Path(target)
            if wav is None:
                path.unlink(missing_ok=True)
                return
            tmp = path.with_suffix(".tmp")
            tmp.write_bytes(wav)
            os.chmod(tmp, 0o644)
            os.replace(tmp, path)
        except OSError as e:
            logger.error("Could not copy the menu recording to Asterisk at %s: %s", target, e)

    async def delete_prompt(self, slot: str) -> bool:
        path = self.prompt_path(slot)
        if path is None:
            raise KeyError(slot)
        async with self._lock:
            existed = self._prompts[slot] is not None
            self._prompts[slot] = None
            self._pcm_cache.pop(slot, None)
            path.unlink(missing_ok=True)
            self._sync_asterisk_copy(slot, None)
            if existed:
                self._persist()
            return existed

    def load_prompt_pcm(self, slot: str, rate: int | None = None) -> bytes | None:
        """The recording as PCM16 at the phone line's rate, or None. Cached until the file changes."""
        path = self.prompt_path(slot)
        if path is None or not path.exists():
            return None
        rate = rate or config.AUDIOSOCKET_SAMPLE_RATE_HZ
        mtime = path.stat().st_mtime
        cached = self._pcm_cache.get(slot)
        if cached and cached[0] == mtime and cached[1] == rate:
            return cached[2]
        try:
            with wave.open(str(path), "rb") as wav:
                pcm = wav.readframes(wav.getnframes())
        except (wave.Error, OSError):
            logger.exception("Could not read the %s recording %s", slot, path)
            return None
        if rate != LINE_RATE_HZ:
            pcm, _ = resample_pcm16(pcm, LINE_RATE_HZ, rate)
        self._pcm_cache[slot] = (mtime, rate, pcm)
        return pcm
