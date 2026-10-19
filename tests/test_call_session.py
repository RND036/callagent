from __future__ import annotations

import asyncio
import uuid as uuid_module

import pytest

import config
from pbx_bridge import call_session as call_session_module
from pbx_bridge.audiosocket_protocol import AudioSocketAdapter, StartEvent
from pbx_bridge.call_session import CallSession
from tools.context import CallContext


class FakePbxConn:
    """Fake AudioSocketConnection: recv_packet() pops the next queued (msg_type, payload) tuple, returning None
    once exhausted -- mimicking a connection that closes when the queue runs out."""

    def __init__(self, packets: list[tuple[int, bytes]]) -> None:
        self._packets = list(packets)
        self.sent: list[tuple[int, bytes]] = []

    async def recv_packet(self) -> tuple[int, bytes] | None:
        if not self._packets:
            return None
        return self._packets.pop(0)

    async def send_packet(self, msg_type: int, payload: bytes) -> None:
        self.sent.append((msg_type, payload))


def make_session(pbx_conn) -> CallSession:
    return CallSession(pbx_conn, gemini_client=None, model="test-model", store=None, mcp_client=None)


def make_ctx(**overrides) -> CallContext:
    fields = dict(
        store=None, mcp=None, call_sid="call-1", other_leg_call_id=None, account_id=None, caller_number=None
    )
    fields.update(overrides)
    return CallContext(**fields)


def uuid_packet(call_uuid: uuid_module.UUID | None = None) -> tuple[int, bytes]:
    return 0x01, (call_uuid or uuid_module.uuid4()).bytes


def audio_packet(payload: bytes, sample_rate: int = 8000) -> tuple[int, bytes]:
    msg_type = {8000: 0x10, 16000: 0x12}[sample_rate]
    return msg_type, payload


class _CapturingRunAgent:
    """Stands in for run_agent in run(): records the CallContext, then ends the call straight away."""

    contexts: list[CallContext] = []

    async def __call__(self, line, ctx, transcript, client, model, **kw):
        self.contexts.append(ctx)
        raise RuntimeError("stop the call here")


@pytest.mark.asyncio
@pytest.mark.parametrize("registered_language, expected", [("en", "en"), ("ta", "ta"), (None, "si")])
async def test_run_uses_ivr_language_registered_by_dialplan(monkeypatch, registered_language, expected) -> None:
    from data.call_manager import CallManager

    call_uuid = uuid_module.uuid4()
    manager = CallManager()
    if registered_language:
        manager.register_pending_call(str(call_uuid), "0781827743", registered_language)
    capture = _CapturingRunAgent()
    capture.contexts = []
    monkeypatch.setattr(call_session_module, "run_agent", capture)

    session = CallSession(
        FakePbxConn([uuid_packet(call_uuid)]), gemini_client=None, model="test-model", store=None, call_manager=manager
    )
    await session.run()

    [ctx] = capture.contexts
    assert ctx.language == expected
    assert ctx.caller_number == ("0781827743" if registered_language else None)


def test_init_uses_audiosocket_adapter() -> None:
    session = make_session(FakePbxConn([]))
    assert isinstance(session._control, AudioSocketAdapter)


@pytest.mark.asyncio
async def test_wait_for_start_parses_uuid_packet() -> None:
    call_uuid = uuid_module.uuid4()
    session = make_session(FakePbxConn([uuid_packet(call_uuid)]))
    start = await session._wait_for_start()
    assert start == StartEvent(
        stream_sid=str(call_uuid),
        call_sid=str(call_uuid),
        other_leg_call_id=None,
        account_id=None,
        caller_id_number=None,
    )


@pytest.mark.asyncio
async def test_wait_for_start_malformed_uuid_falls_back_to_default() -> None:
    session = make_session(FakePbxConn([(0x01, b"too-short")]))
    start = await session._wait_for_start()
    assert start is not None
    assert start.call_sid is None


@pytest.mark.asyncio
async def test_wait_for_start_audio_first_falls_back_to_default() -> None:
    session = make_session(FakePbxConn([audio_packet(b"\x00\x01\x02\x03")]))
    start = await session._wait_for_start()
    assert start is not None
    assert start.call_sid is None


@pytest.mark.asyncio
async def test_wait_for_start_socket_closed_returns_none() -> None:
    session = make_session(FakePbxConn([]))
    start = await session._wait_for_start()
    assert start is None


class _ScriptedAgent:
    """Stands in for run_agent in run(): feeds a fixed conversation into the transcript, then ends the call."""

    async def __call__(self, line, ctx, transcript, client, model, **kw):
        from gemini_session.live_session import AgentResult

        await line.receive()  # the caller's first audio goes to the recording
        await transcript.add("ai", "Hello! ")
        await transcript.add("ai", "How can I help?")
        await transcript.add("caller", "I want ")
        await transcript.add("caller", "a manicure")
        await transcript.add("ai", "Sure.")
        await transcript.flush()
        return AgentResult(reason="completed")


class _HoldOpenPbxConn(FakePbxConn):
    """Serves its packets, then stays connected (like a live caller) until cancelled."""

    async def recv_packet(self):
        if not self._packets:
            await asyncio.Event().wait()
        return self._packets.pop(0)


@pytest.mark.asyncio
async def test_run_saves_recording_and_merged_transcript(monkeypatch, tmp_path) -> None:
    import wave

    from data.call_manager import CallManager

    monkeypatch.setattr(call_session_module, "run_agent", _ScriptedAgent())
    monkeypatch.setattr(config, "RECORDINGS_DIR", tmp_path)
    monkeypatch.setattr(config, "RECORDINGS_ENABLED", True)

    call_uuid = uuid_module.uuid4()
    manager = CallManager(recordings_dir=tmp_path)
    pbx = _HoldOpenPbxConn([uuid_packet(call_uuid), audio_packet(b"\x10\x00" * 160)])
    session = CallSession(pbx, gemini_client=None, model="m", store=None, call_manager=manager)
    await session.run()

    [record] = await manager.get_call_history()
    assert record["recording_file"] == f"{call_uuid}.wav"
    with wave.open(str(tmp_path / record["recording_file"]), "rb") as wav:
        assert wav.getframerate() == config.AUDIOSOCKET_SAMPLE_RATE_HZ
        assert wav.getnframes() >= 160
    assert manager.recording_path(str(call_uuid)) == tmp_path / f"{call_uuid}.wav"

    spoken = [a["action"] for a in record["actions"] if a["details"].get("speaker")]
    assert spoken == ["AI Receptionist: Hello! How can I help?", "Caller: I want a manicure", "AI Receptionist: Sure."]


@pytest.mark.asyncio
async def test_run_without_recordings_enabled_saves_no_file(monkeypatch, tmp_path) -> None:
    from data.call_manager import CallManager

    monkeypatch.setattr(call_session_module, "run_agent", _ScriptedAgent())
    monkeypatch.setattr(config, "RECORDINGS_DIR", tmp_path)
    monkeypatch.setattr(config, "RECORDINGS_ENABLED", False)

    manager = CallManager(recordings_dir=tmp_path)
    pbx = _HoldOpenPbxConn([uuid_packet(), audio_packet(b"\x10\x00" * 160)])
    await CallSession(pbx, gemini_client=None, model="m", store=None, call_manager=manager).run()

    [record] = await manager.get_call_history()
    assert record["recording_file"] is None
    assert list(tmp_path.glob("*.wav")) == []


class _DroppedAgent:
    """Stands in for run_agent: the caller gave a name and a service, then Gemini stayed unreachable."""

    async def __call__(self, line, ctx, transcript, client, model, **kw):
        from gemini_session.live_session import AgentResult

        await line.receive()
        ctx.customer.update(name="Nimali", service="Gel Manicure")
        return AgentResult(reason="error", error="1011 internal error", reconnects=4, greeted=True)


class _FakeMessageStore:
    def __init__(self) -> None:
        self.messages: list[dict] = []

    async def take_message(self, **kw) -> dict:
        self.messages.append(kw)
        return kw


@pytest.mark.asyncio
async def test_dropped_call_is_saved_as_dropped_and_leaves_a_callback(monkeypatch, tmp_path) -> None:
    from data.call_manager import CallManager

    monkeypatch.setattr(call_session_module, "run_agent", _DroppedAgent())
    monkeypatch.setattr(config, "RECORDINGS_DIR", tmp_path)
    monkeypatch.setattr(config, "RECORDINGS_ENABLED", True)

    call_uuid = uuid_module.uuid4()
    manager = CallManager(recordings_dir=tmp_path)
    manager.register_pending_call(str(call_uuid), "0771234567", "si")
    store = _FakeMessageStore()
    pbx = _HoldOpenPbxConn([uuid_packet(call_uuid), audio_packet(b"\x10\x00" * 160)])
    await CallSession(pbx, gemini_client=None, model="m", store=store, call_manager=manager).run()

    [record] = await manager.get_call_history()
    assert record["status"] == "dropped" and record["end_reason"] == "error" and record["reconnects"] == 4
    assert record["customer"] == {"name": "Nimali", "service": "Gel Manicure"}
    assert record["recording_file"] == f"{call_uuid}.wav"
    [msg] = store.messages
    assert msg["caller_phone"] == "0771234567" and msg["caller_name"] == "Nimali" and msg["urgent"]
    assert "Gel Manicure" in msg["message"]


@pytest.mark.asyncio
async def test_record_and_recording_are_saved_even_if_closing_the_line_fails(monkeypatch, tmp_path) -> None:
    from data.call_manager import CallManager
    from pbx_bridge.asterisk_transport import AsteriskTransport

    async def broken_close(self):
        raise ConnectionResetError("socket gone")

    monkeypatch.setattr(AsteriskTransport, "close", broken_close)
    monkeypatch.setattr(call_session_module, "run_agent", _ScriptedAgent())
    monkeypatch.setattr(config, "RECORDINGS_DIR", tmp_path)
    monkeypatch.setattr(config, "RECORDINGS_ENABLED", True)

    manager = CallManager(recordings_dir=tmp_path)
    pbx = _HoldOpenPbxConn([uuid_packet(), audio_packet(b"\x10\x00" * 160)])
    session = CallSession(pbx, gemini_client=None, model="m", store=None, call_manager=manager)
    await session.run()

    [record] = await manager.get_call_history()
    assert record["status"] == "completed" and record["recording_file"]
    assert list(tmp_path.glob("*.pcm")) == []


class _LiveConn:
    """A connection that stays open: packets are pushed in by the test; None (hang-up) is pushed to end it."""

    def __init__(self) -> None:
        self.queue: asyncio.Queue = asyncio.Queue()
        self.sent: list[tuple[int, bytes]] = []

    async def recv_packet(self):
        return await self.queue.get()

    async def send_packet(self, msg_type: int, payload: bytes) -> None:
        self.sent.append((msg_type, payload))


async def _menu_store(tmp_path, enabled=True):
    from data.ivr_store import IvrStore
    from tests.test_ivr_store import make_wav

    store = IvrStore(tmp_path / "ivr.json", tmp_path / "prompts")
    await store.save_prompt("menu", make_wav(rate=8000, channels=1, seconds=0.2), "menu.wav")
    await store.update({"enabled": enabled, "wait_seconds": 2})
    return store


@pytest.mark.asyncio
async def test_menu_key_sets_language_and_is_not_forwarded(monkeypatch, tmp_path) -> None:
    from data.call_manager import CallManager

    call_uuid = uuid_module.uuid4()
    manager = CallManager()
    manager.register_pending_call(str(call_uuid), "0781827743", None)  # the dialplan sent no lang=
    capture = _CapturingRunAgent()
    capture.contexts = []
    seen_dtmf: list[int] = []

    async def agent(line, ctx, transcript, client, model, **kw):
        capture.contexts.append(ctx)
        seen_dtmf.append(line.dtmf.qsize())
        raise RuntimeError("stop")

    monkeypatch.setattr(call_session_module, "run_agent", agent)
    monkeypatch.setattr(config, "RECORDINGS_ENABLED", False)
    conn = _LiveConn()
    session = CallSession(conn, gemini_client=None, model="m", store=None, call_manager=manager,
                          ivr_store=await _menu_store(tmp_path))
    await conn.queue.put(uuid_packet(call_uuid))
    task = asyncio.create_task(session.run())
    await asyncio.sleep(0.2)
    await conn.queue.put((0x03, b"2"))
    await asyncio.wait_for(task, timeout=10)

    [ctx] = capture.contexts
    assert ctx.language == "ta"
    assert seen_dtmf == [0]  # the menu key never reaches the model as a keypad note
    assert any(t == 0x10 for t, _ in conn.sent)  # the menu recording went out on the line


@pytest.mark.asyncio
async def test_dialplan_language_skips_menu(monkeypatch, tmp_path) -> None:
    from data.call_manager import CallManager

    call_uuid = uuid_module.uuid4()
    manager = CallManager()
    manager.register_pending_call(str(call_uuid), None, "en")
    capture = _CapturingRunAgent()
    capture.contexts = []
    monkeypatch.setattr(call_session_module, "run_agent", capture)
    conn = _LiveConn()
    await conn.queue.put(uuid_packet(call_uuid))
    session = CallSession(conn, gemini_client=None, model="m", store=None, call_manager=manager,
                          ivr_store=await _menu_store(tmp_path))
    await asyncio.wait_for(session.run(), timeout=10)
    assert capture.contexts[0].language == "en"


@pytest.mark.asyncio
async def test_hangup_during_menu_is_completed_without_callback(monkeypatch, tmp_path) -> None:
    from data.call_manager import CallManager

    call_uuid = uuid_module.uuid4()
    manager = CallManager()
    manager.register_pending_call(str(call_uuid), "0781827743", None)

    async def agent(*args, **kwargs):  # pragma: no cover - must not run
        raise AssertionError("the agent must not start when the caller hung up in the menu")

    monkeypatch.setattr(call_session_module, "run_agent", agent)
    monkeypatch.setattr(config, "RECORDINGS_ENABLED", False)
    conn = _LiveConn()
    await conn.queue.put(uuid_packet(call_uuid))
    session = CallSession(conn, gemini_client=None, model="m", store=None, call_manager=manager,
                          ivr_store=await _menu_store(tmp_path))
    task = asyncio.create_task(session.run())
    await asyncio.sleep(0.2)
    await conn.queue.put(None)
    await asyncio.wait_for(task, timeout=10)
    [call] = await manager.get_call_history()
    assert call["status"] == "completed" and call["end_reason"] == "caller_hung_up_in_menu"


@pytest.mark.parametrize("pressed, expected", [(b"3", "en"), (None, "ta")])
@pytest.mark.asyncio
async def test_menu_key_mid_call_replays_menu_and_continues(monkeypatch, tmp_path, pressed, expected) -> None:
    from data.call_manager import CallManager
    from gemini_session.live_session import AgentResult

    call_uuid = uuid_module.uuid4()
    manager = CallManager()
    manager.register_pending_call(str(call_uuid), "0781827743", "ta")  # chosen before: no first menu
    calls: list[tuple[str, bool]] = []
    store = await _menu_store(tmp_path)
    store._cfg["wait_seconds"], store._cfg["max_tries"] = 2, 1

    async def agent(line, ctx, transcript, client, model, language_changed=False, **kw):
        calls.append((ctx.language, language_changed))
        return AgentResult(reason="completed" if language_changed else "language_menu")

    monkeypatch.setattr(call_session_module, "run_agent", agent)
    monkeypatch.setattr(config, "RECORDINGS_ENABLED", False)
    conn = _LiveConn()
    session = CallSession(conn, gemini_client=None, model="m", store=None, call_manager=manager, ivr_store=store)
    await conn.queue.put(uuid_packet(call_uuid))
    task = asyncio.create_task(session.run())
    await asyncio.sleep(0.2)
    if pressed:
        await conn.queue.put((0x03, pressed))
    await asyncio.wait_for(task, timeout=10)

    assert calls == [("ta", False), (expected, True)]
