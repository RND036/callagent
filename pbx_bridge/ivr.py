"""The language menu, played by nailagent over the AudioSocket line before the AI answers: plays the uploaded
recording, waits for a key, and returns the language that key stands for -- see data/ivr_store.py for the settings
and the dashboard's "Language Menu" tab."""
from __future__ import annotations

import asyncio
import contextlib
import logging
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class IvrChoice:
    language: str
    digit: str | None = None  # the key pressed; None when nothing valid was pressed
    tries: int = 1
    timed_out: bool = False  # ran out of tries: the default language was used


def _drain(queue: asyncio.Queue) -> None:
    while not queue.empty():
        queue.get_nowait()


async def _listen(transport) -> None:
    """Keep reading the line so key presses reach transport.dtmf; the caller's audio is not needed here."""
    while await transport.receive() is not None:
        pass


async def run_language_menu(transport, cfg: dict[str, Any], menu_pcm: bytes, invalid_pcm: bytes | None = None) -> IvrChoice | None:
    """Plays the menu until the caller presses a mapped key. Returns None if the caller hangs up first."""
    keys = {o["digit"]: o["language"] for o in cfg["options"]}
    max_tries = int(cfg["max_tries"])
    wait = float(cfg["wait_seconds"])
    listener = asyncio.create_task(_listen(transport), name="ivr-listener")
    try:
        pcm = menu_pcm
        for attempt in range(1, max_tries + 1):
            _drain(transport.dtmf)
            await transport.play(pcm)
            digit = await _wait_for_digit(transport, listener, wait)
            if digit is _HUNG_UP:
                return None
            if digit is not None:
                await transport.clear()  # stop the prompt the moment a key is pressed
                if digit in keys:
                    logger.info("Language menu: key %s -> %s (try %d)", digit, keys[digit], attempt)
                    return IvrChoice(keys[digit], digit, attempt)
                logger.info("Language menu: key %s is not an option (try %d)", digit, attempt)
                pcm = invalid_pcm or menu_pcm
            else:
                pcm = menu_pcm
        logger.info("Language menu: no valid key after %d tries; using %s", max_tries, cfg["default_language"])
        return IvrChoice(cfg["default_language"], None, max_tries, timed_out=True)
    finally:
        listener.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await listener


_HUNG_UP = object()


async def _wait_for_digit(transport, listener: asyncio.Task, wait: float):
    """A pressed key, None when the prompt finished and `wait` seconds passed with no key, or _HUNG_UP."""
    played = asyncio.create_task(transport.wait_played())
    key = asyncio.create_task(transport.dtmf.get())
    try:
        # Keys are accepted while the prompt is still playing (callers who know the menu press early).
        done, _ = await asyncio.wait({played, key, listener}, return_when=asyncio.FIRST_COMPLETED)
        if key in done:
            return key.result()
        if listener in done:
            return _HUNG_UP
        done, _ = await asyncio.wait({key, listener}, timeout=wait, return_when=asyncio.FIRST_COMPLETED)
        if key in done:
            return key.result()
        if listener in done:
            return _HUNG_UP
        return None
    finally:
        for task in (played, key):
            if not task.done():
                task.cancel()
        await asyncio.gather(played, key, return_exceptions=True)
