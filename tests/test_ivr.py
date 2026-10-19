from __future__ import annotations

import asyncio

import pytest

from pbx_bridge.ivr import run_language_menu

CFG = {
    "options": [{"digit": "1", "language": "si"}, {"digit": "2", "language": "ta"}, {"digit": "3", "language": "en"}],
    "default_language": "si", "max_tries": 3, "wait_seconds": 2,
}


class FakeLine:
    """The parts of AsteriskTransport the menu uses. `script` is a list of (delay, digit | 'hangup')."""

    def __init__(self, script, prompt_seconds=0.05) -> None:
        self.dtmf: asyncio.Queue[str] = asyncio.Queue()
        self.played: list[bytes] = []
        self.cleared = 0
        self._script = list(script)
        self._hung_up = asyncio.Event()
        self._prompt_seconds = prompt_seconds
        self._playing = asyncio.Event()
        self._playing.set()
        self._clear_event = asyncio.Event()
        self._task = None

    async def play(self, pcm: bytes) -> None:
        self.played.append(pcm)
        self._playing.clear()
        self._clear_event.clear()
        asyncio.get_running_loop().call_later(self._prompt_seconds, self._playing.set)
        if self._script and self._task is None:
            self._task = asyncio.create_task(self._run_script())

    async def _run_script(self) -> None:
        for delay, item in self._script:
            await asyncio.sleep(delay)
            if item == "hangup":
                self._hung_up.set()
            else:
                self.dtmf.put_nowait(item)

    async def wait_played(self) -> None:
        await self._playing.wait()

    async def clear(self) -> None:
        self.cleared += 1
        self._playing.set()

    async def receive(self):
        await self._hung_up.wait()
        return None


@pytest.mark.asyncio
async def test_valid_key_returns_language_and_stops_prompt() -> None:
    line = FakeLine([(0.01, "2")], prompt_seconds=5)
    choice = await run_language_menu(line, CFG, b"menu")
    assert (choice.language, choice.digit, choice.tries, choice.timed_out) == ("ta", "2", 1, False)
    assert line.cleared == 1


@pytest.mark.asyncio
async def test_invalid_key_plays_invalid_prompt_then_accepts_valid() -> None:
    line = FakeLine([(0.01, "9"), (0.1, "3")])
    choice = await run_language_menu(line, CFG, b"menu", b"invalid")
    assert choice.language == "en" and choice.tries == 2
    assert line.played == [b"menu", b"invalid"]


@pytest.mark.asyncio
async def test_no_key_uses_default_after_all_tries() -> None:
    cfg = {**CFG, "wait_seconds": 2, "max_tries": 2, "default_language": "en"}
    line = FakeLine([])
    # shorten the wait so the test is quick
    cfg["wait_seconds"] = 2
    choice = await asyncio.wait_for(run_language_menu(line, cfg, b"menu"), timeout=10)
    assert choice.language == "en" and choice.timed_out and choice.digit is None
    assert line.played == [b"menu", b"menu"]


@pytest.mark.asyncio
async def test_hangup_returns_none() -> None:
    line = FakeLine([(0.02, "hangup")], prompt_seconds=5)
    assert await run_language_menu(line, CFG, b"menu") is None
