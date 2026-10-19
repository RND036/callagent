"""Salon FAQ: question/answer pairs staff edit on the dashboard. The AI may state these facts (and only these,
besides what its tools return) on a call; anything else becomes "the team will confirm"."""
from __future__ import annotations

import asyncio
import json
import logging
import uuid
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

MAX_PROMPT_CHARS = 6000

# Written from the salon's call script. Entries with an empty answer are skipped by the AI until staff fill them in.
SEED_FAQS = [
    ("Where are your branches and what are their addresses?",
     "Nugegoda: 309 Kotte Road, Nugegoda 10250.\nHokandara: (address not set yet)"),
    ("What are your opening hours?", "Daily from 9:00 AM to 7:00 PM (open 7 days a week)."),
    ("Can you remove extensions that another salon did?",
     "Yes, a technician checks them and advises the right removal service. Staff confirm the details."),
    ("Why does the price of gel extensions vary?",
     "The price depends on the length and the design (plain or with nail art), so the team confirms it."),
    ("Can I send a photo of the design I want?",
     "Yes, send the photo on WhatsApp to the salon's WhatsApp number and the team will check it."),
]


class FaqStore:
    def __init__(self, path: Path | None = None) -> None:
        self._path = path
        self._lock = asyncio.Lock()
        self._faqs: list[dict[str, Any]] = []
        if self._path and self._path.exists():
            try:
                data = json.loads(self._path.read_text())
                if isinstance(data, list):
                    self._faqs = [self._clean(f) for f in data if isinstance(f, dict)]
            except Exception as e:  # noqa: BLE001
                logger.warning("Failed loading FAQs from %s: %s", self._path, e)
        elif self._path:
            self._faqs = [self._clean({"question": q, "answer": a}) for q, a in SEED_FAQS]
            self._persist()

    @staticmethod
    def _clean(f: dict[str, Any]) -> dict[str, Any]:
        return {
            "faq_id": str(f.get("faq_id") or uuid.uuid4().hex[:8]),
            "question": str(f.get("question", "")).strip(),
            "answer": str(f.get("answer", "")).strip(),
            "enabled": bool(f.get("enabled", True)),
        }

    def _persist(self) -> None:
        if not self._path:
            return
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._path.write_text(json.dumps(self._faqs, indent=2, ensure_ascii=False))
        except Exception as e:  # noqa: BLE001
            logger.error("Failed persisting FAQs to %s: %s", self._path, e)

    async def list_faqs(self) -> list[dict[str, Any]]:
        async with self._lock:
            return [dict(f) for f in self._faqs]

    async def upsert(self, data: dict[str, Any], faq_id: str | None = None) -> dict[str, Any]:
        """Add (no faq_id) or update. Raises ValueError for a blank question, KeyError for an unknown id."""
        async with self._lock:
            if faq_id is None:
                faq = self._clean({k: data.get(k) for k in ("question", "answer", "enabled") if k in data})
                if not faq["question"]:
                    raise ValueError("A question is required.")
                self._faqs.append(faq)
            else:
                existing = next((f for f in self._faqs if f["faq_id"] == faq_id), None)
                if existing is None:
                    raise KeyError(faq_id)
                merged = self._clean({**existing, **{k: v for k, v in data.items() if k != "faq_id"}})
                if not merged["question"]:
                    raise ValueError("A question is required.")
                existing.update(merged)
                faq = existing
            self._persist()
            return dict(faq)

    async def delete(self, faq_id: str) -> bool:
        async with self._lock:
            before = len(self._faqs)
            self._faqs = [f for f in self._faqs if f["faq_id"] != faq_id]
            changed = len(self._faqs) < before
            if changed:
                self._persist()
            return changed


def faq_prompt_lines(faqs: list[dict[str, Any]], limit: int = MAX_PROMPT_CHARS) -> list[str]:
    """"Q: ...\nA: ..." blocks of the enabled FAQs that have an answer, cut off at `limit` characters."""
    blocks, used = [], 0
    for f in faqs:
        if not f.get("enabled", True) or not f.get("answer", "").strip():
            continue
        block = f"Q: {f['question']}\nA: {f['answer']}"
        if used + len(block) > limit:
            logger.warning("FAQ is longer than %d characters; the rest is left out of the AI's instructions", limit)
            break
        blocks.append(block)
        used += len(block)
    return blocks
