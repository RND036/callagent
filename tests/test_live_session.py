"""run_agent / LiveLink / AsteriskTransport: the pieces that keep a call from dropping."""
from __future__ import annotations

import asyncio

import pytest
from google.genai import errors, types

import config
from data.call_manager import TranscriptLog
from gemini_session import live_session
from gemini_session.live_session import LiveLink, run_agent
from gemini_session.persona import no_response_note, resume_note, still_there_note
from pbx_bridge.asterisk_transport import AsteriskTransport
from tools.context import CallContext

PCM = b"\x10\x00" * 2400  # 0.1 s @ 24 kHz


def audio_msg(text: str | None = None) -> types.LiveServerMessage:
    content = types.LiveServerContent(
        model_turn=types.Content(parts=[types.Part(inline_data=types.Blob(data=PCM, mime_type="audio/pcm"))]),
        output_transcription=types.Transcription(text=text) if text else None,
    )
    return types.LiveServerMessage(server_content=content)


def turn_complete() -> types.LiveServerMessage:
    return types.LiveServerMessage(server_content=types.LiveServerContent(turn_complete=True))


def end_call_msg() -> types.LiveServerMessage:
    call = types.FunctionCall(id="1", name="end_call", args={"reason": "done"})
    return types.LiveServerMessage(tool_call=types.LiveServerToolCall(function_calls=[call]))


def resumption(handle: str) -> types.LiveServerMessage:
    return types.LiveServerMessage(
        session_resumption_update=types.LiveServerSessionResumptionUpdate(new_handle=handle, resumable=True)
    )


def go_away() -> types.LiveServerMessage:
    return types.LiveServerMessage(go_away=types.LiveServerGoAway(time_left="30s"))


class FakeLiveSession:
    """Plays its script (messages, or an exception to raise), then stays open like a live connection."""

    def __init__(self, script) -> None:
        self.script = list(script)
        self.notes: list[str] = []
        self.audio: list[bytes] = []
        self.tool_responses: list = []

    async def send_client_content(self, turns, turn_complete=True) -> None:
        self.notes.append(turns.parts[0].text)

    async def send_realtime_input(self, audio=None, text=None, **kw) -> None:
        if audio is not None:
            self.audio.append(audio.data)
        if text is not None:
            self.notes.append(text)

    async def send_tool_response(self, function_responses) -> None:
        self.tool_responses.extend(function_responses)

    async def receive(self):
        while self.script:
            item = self.script.pop(0)
            if isinstance(item, BaseException):
                raise item
            yield item
            await asyncio.sleep(0)
        await asyncio.Event().wait()


class FakeClient:
    """One script per connect; a script that is an exception makes that connect fail."""

    def __init__(self, *scripts) -> None:
        self.sessions = [s if isinstance(s, BaseException) else FakeLiveSession(s) for s in scripts]
        self.configs: list[types.LiveConnectConfig] = []
        self.aio = self
        self.live = self

    def connect(self, model, config):
        self.configs.append(config)
        session = self.sessions[len(self.configs) - 1]

        class _Ctx:
            async def __aenter__(self_inner):
                if isinstance(session, BaseException):
                    raise session
                return session

            async def __aexit__(self_inner, *exc):
                return None

        return _Ctx()


class FakeLine:
    def __init__(self, caller_audio: bool = False) -> None:
        self.dtmf: asyncio.Queue[str] = asyncio.Queue()
        self.sent: list[bytes] = []
        self.cleared = 0
        self._hold = not caller_audio

    async def receive(self):
        if self._hold:
            await asyncio.Event().wait()
        return None  # the caller hung up

    async def send(self, pcm: bytes) -> None:
        self.sent.append(pcm)

    async def clear(self) -> None:
        self.cleared += 1

    async def event(self, data: dict) -> None:
        pass

    async def close(self) -> None:
        pass


def make_ctx() -> CallContext:
    return CallContext(store=None, mcp=None, call_sid="c1", other_leg_call_id=None, account_id=None,
                       caller_number=None, language="en")


@pytest.fixture(autouse=True)
def fast(monkeypatch):
    monkeypatch.setattr(live_session, "GOODBYE_SETTLE_SECONDS", 0.05)
    monkeypatch.setattr(live_session, "GOODBYE_GRACE_SECONDS", 0.2)


async def agent(client, line=None, ctx=None, **kw):
    ctx = ctx or make_ctx()
    result = await asyncio.wait_for(
        run_agent(line or FakeLine(), ctx, TranscriptLog(None, "c1"), client, "m", **kw), timeout=10
    )
    return result, ctx


@pytest.mark.asyncio
async def test_greets_once_and_hangs_up_after_end_call():
    client = FakeClient([audio_msg("Hello! "), turn_complete(), audio_msg("Goodbye!"), end_call_msg()])
    line = FakeLine()
    result, ctx = await agent(client, line)
    assert result.reason == "completed" and ctx.hangup_requested
    assert len(client.configs) == 1
    assert len(client.sessions[0].notes) == 1 and "Greet the caller" in client.sessions[0].notes[0]
    assert line.sent  # agent audio reached the line
    assert client.sessions[0].tool_responses[0].response == {"status": "call_ending"}


@pytest.mark.asyncio
async def test_config_enables_resumption_and_compression():
    client = FakeClient([end_call_msg()])
    await agent(client)
    cfg = client.configs[0]
    assert cfg.session_resumption is not None and cfg.context_window_compression is not None


def drop(code: int = 1011) -> errors.APIError:
    return errors.APIError(code, {"error": {"message": "x"}})


def caller_said(text: str) -> types.LiveServerMessage:
    return types.LiveServerMessage(server_content=types.LiveServerContent(
        input_transcription=types.Transcription(text=text)))


def booking_msg() -> types.LiveServerMessage:
    call = types.FunctionCall(id="b1", name="create_booking", args={"customer_name": "Nimali"})
    return types.LiveServerMessage(tool_call=types.LiveServerToolCall(function_calls=[call]))


@pytest.fixture
def booked(monkeypatch):
    """create_booking succeeds without a store."""
    from tools import registry

    async def fake_create(args, ctx):
        return {"booking_id": "B7", "service_name": "Gel Manicure", "date": "2026-10-20", "time": "15:00",
                "status": "requested"}

    monkeypatch.setitem(registry._HANDLERS, "create_booking", fake_create)


@pytest.mark.asyncio
async def test_reconnects_with_handle_after_connection_drop():
    client = FakeClient(
        [audio_msg("Hello! "), turn_complete(), resumption("h1"), drop()],
        [audio_msg("Goodbye!"), end_call_msg()],
    )
    result, _ = await agent(client)
    assert result.reason == "completed" and result.reconnects == 1
    assert len(client.configs) == 2
    assert client.configs[1].session_resumption.handle == "h1"
    assert client.sessions[1].notes == []  # the handle holds everything: no second greeting, no note


@pytest.mark.asyncio
async def test_resume_hands_over_the_turns_said_after_the_handle():
    client = FakeClient(
        [resumption("h1"), audio_msg("Hello! "), turn_complete(), caller_said("My name is Nimali"), drop()],
        [audio_msg("Goodbye!"), end_call_msg()],
    )
    result, _ = await agent(client)
    assert result.reconnects == 1 and client.configs[1].session_resumption.handle == "h1"
    [note] = client.sessions[1].notes
    assert "You: Hello!" in note and "Caller: My name is Nimali" in note
    assert "Do not greet again" in note and "English only" in note  # the call's language


@pytest.mark.asyncio
async def test_first_connect_failure_is_retried_and_greets_once():
    client = FakeClient(drop(1006), [audio_msg("Hello! "), turn_complete(), end_call_msg()])
    result, _ = await agent(client)
    assert result.reason == "completed" and result.reconnects == 1
    assert len(client.sessions[1].notes) == 1 and "Greet the caller" in client.sessions[1].notes[0]


@pytest.mark.asyncio
async def test_outage_longer_than_the_reconnect_window_ends_the_call(monkeypatch):
    monkeypatch.setattr(config, "RECONNECT_WINDOW_SECONDS", 0.3)
    monkeypatch.setattr(live_session, "RECONNECT_BACKOFF_SECONDS", (0.2,))
    client = FakeClient(*[drop() for _ in range(10)])
    result, _ = await agent(client)
    assert result.reason == "error"
    assert 2 <= len(client.configs) < 10


@pytest.mark.asyncio
async def test_renewals_do_not_use_up_the_reconnect_budget(monkeypatch):
    monkeypatch.setattr(live_session, "RECONNECT_BACKOFF_SECONDS", (0.01,))
    renewals = [[resumption(f"h{i}"), go_away()] for i in range(6)]
    client = FakeClient(*renewals, [drop()], [end_call_msg()])
    result, _ = await agent(client)
    assert result.reason == "completed" and result.reconnects == 7


@pytest.mark.asyncio
async def test_refused_handle_reconnects_fresh_with_the_recap():
    client = FakeClient(
        [audio_msg("Your name please?"), turn_complete(), resumption("old"), drop()],
        drop(1007),  # the server refuses the handle
        [audio_msg("Goodbye!"), end_call_msg()],
    )
    result, _ = await agent(client)
    assert result.reason == "completed"
    assert client.configs[1].session_resumption.handle == "old"
    assert client.configs[2].session_resumption.handle is None
    assert "Your name please?" in client.configs[2].system_instruction
    assert client.sessions[2].notes == [resume_note("en")]


@pytest.mark.asyncio
async def test_handle_failing_again_falls_back_to_a_fresh_session(monkeypatch):
    monkeypatch.setattr(live_session, "RECONNECT_BACKOFF_SECONDS", (0.01,))
    client = FakeClient(
        [audio_msg("Your name please?"), turn_complete(), resumption("h"), drop()],
        drop(1011), drop(1011),  # resuming keeps failing
        [audio_msg("Goodbye!"), end_call_msg()],
    )
    result, _ = await agent(client)
    assert result.reason == "completed"
    assert [c.session_resumption.handle for c in client.configs] == [None, "h", "h", None]
    assert client.sessions[3].notes == [resume_note("en")]


@pytest.mark.asyncio
async def test_drop_before_anything_was_said_greets_again():
    client = FakeClient([drop()], [audio_msg("Hello! "), turn_complete(), end_call_msg()])
    result, _ = await agent(client)
    assert result.reason == "completed"
    assert all(len(s.notes) == 1 and "Greet the caller" in s.notes[0] for s in client.sessions)


@pytest.mark.asyncio
async def test_drop_after_end_call_is_not_an_error():
    client = FakeClient([audio_msg("Goodbye!"), end_call_msg(), drop()])
    result, ctx = await agent(client)
    assert result.reason == "completed" and ctx.hangup_requested and len(client.configs) == 1


@pytest.mark.asyncio
async def test_tool_results_become_customer_details_in_the_recap(booked):
    client = FakeClient(
        [audio_msg("Booking it. "), booking_msg(), drop()],
        [audio_msg("Goodbye!"), end_call_msg()],
    )
    result, ctx = await agent(client)
    assert ctx.customer["name"] == "Nimali" and ctx.customer["booking_id"] == "B7"
    recap = client.configs[1].system_instruction
    assert "What you already know about this caller" in recap
    assert "- Name: Nimali" in recap and "- Booking ID: B7" in recap
    assert "Never run a tool again" in recap


@pytest.mark.asyncio
async def test_goaway_deadline_waits_for_a_tool_call_to_finish(monkeypatch):
    from tools import registry

    async def slow_tool(args, ctx):
        await asyncio.sleep(0.3)  # the GoAway deadline (0s) passes meanwhile
        return {"ok": True}

    monkeypatch.setitem(registry._HANDLERS, "quote_price", slow_tool)
    call = types.FunctionCall(id="q1", name="quote_price", args={})
    tool_msg = types.LiveServerMessage(tool_call=types.LiveServerToolCall(function_calls=[call]))
    long_turn = types.LiveServerMessage(go_away=types.LiveServerGoAway(time_left="0s"))
    client = FakeClient([audio_msg("One moment. "), long_turn, tool_msg], [end_call_msg()])
    await agent(client)
    assert [r.id for r in client.sessions[0].tool_responses] == ["q1"]  # sent before the renewal
    assert len(client.configs) == 2


@pytest.mark.asyncio
async def test_reconnect_without_handle_sends_recap_and_resume_note():
    client = FakeClient(
        [audio_msg("Your name please?"), turn_complete(), errors.APIError(1006, {"error": {"message": "x"}})],
        [audio_msg("Goodbye!"), end_call_msg()],
    )
    result, _ = await agent(client)
    assert result.reconnects == 1
    assert client.sessions[1].notes == [resume_note("en")]
    assert "Your name please?" in client.configs[1].system_instruction
    assert "So far in this call" in client.configs[1].system_instruction


@pytest.mark.asyncio
async def test_goaway_renews_the_connection_at_a_turn_boundary():
    client = FakeClient([resumption("h2"), go_away()], [audio_msg("Goodbye!"), end_call_msg()])
    result, _ = await agent(client)
    assert result.reconnects == 1
    assert client.configs[1].session_resumption.handle == "h2"


@pytest.mark.asyncio
async def test_non_retryable_error_ends_the_call():
    client = FakeClient([errors.APIError(1008, {"error": {"message": "bad key"}})])
    result, _ = await agent(client)
    assert result.reason == "error" and len(client.configs) == 1


@pytest.mark.asyncio
async def test_caller_hanging_up_ends_the_agent():
    result, ctx = await agent(FakeClient([audio_msg("Hello! ")]), FakeLine(caller_audio=True))
    assert result.reason == "caller_hung_up" and not ctx.hangup_requested


@pytest.mark.asyncio
async def test_silence_prompts_then_ends_the_call(monkeypatch):
    monkeypatch.setattr(config, "SILENCE_CHECKIN_TIMEOUT_SECONDS", 0.1)
    client = FakeClient([audio_msg("Hello! "), turn_complete()])
    result, ctx = await agent(client)
    notes = client.sessions[0].notes
    assert still_there_note() in notes and no_response_note() in notes
    assert result.reason == "caller_silent" and ctx.hangup_requested


@pytest.mark.asyncio
async def test_keypad_digit_interrupts_and_notifies_the_model():
    client = FakeClient([audio_msg("Hello! ")])
    line = FakeLine()
    line.dtmf.put_nowait("5")
    task = asyncio.create_task(agent(client, line))
    await asyncio.sleep(0.2)
    assert line.cleared == 1
    assert "[caller pressed keypad digit: 5]" in client.sessions[0].notes
    task.cancel()


@pytest.mark.asyncio
async def test_caller_audio_is_buffered_while_reconnecting_and_replayed():
    link = LiveLink()
    await link.send_audio(b"a")
    await link.send_audio(b"b")
    session = FakeLiveSession([])
    await link.attach(session)
    assert session.audio == [b"a", b"b"]


# ---------- AsteriskTransport ----------

class FakeConn:
    def __init__(self, packets=()):
        self.packets = list(packets)
        self.sent: list[tuple[int, bytes]] = []

    async def recv_packet(self):
        if self.packets:
            return self.packets.pop(0)
        await asyncio.Event().wait()

    async def send_packet(self, t, p):
        self.sent.append((t, p))


@pytest.mark.asyncio
async def test_transport_sends_keepalive_frames_when_idle(monkeypatch):
    from pbx_bridge import asterisk_transport as at

    monkeypatch.setattr(at, "KEEPALIVE_SECONDS", 0.03)
    conn = FakeConn()
    t = AsteriskTransport(conn)
    await asyncio.sleep(0.15)
    t._sender.cancel()
    assert len(conn.sent) >= 3
    assert all(m == 0x10 and p == bytes(320) for m, p in conn.sent)  # 20 ms of 8 kHz silence


@pytest.mark.asyncio
async def test_transport_frames_agent_audio_and_clear_drops_it():
    conn = FakeConn()
    t = AsteriskTransport(conn)
    await t.send(PCM * 5)  # 0.5 s @ 24 kHz -> 25 frames @ 8 kHz
    assert t._queue.qsize() == 25
    await t.clear()
    assert t._queue.qsize() == 0
    t._sender.cancel()


@pytest.mark.asyncio
async def test_transport_receive_resamples_queues_dtmf_and_ends_on_hangup():
    conn = FakeConn([(0x03, b"7"), (0x10, b"\x00\x01" * 160), (0x00, b"")])
    t = AsteriskTransport(conn)
    chunk = await t.receive()
    assert 630 <= len(chunk) <= 640  # ~20 ms @ 16 kHz (resampler carries state)
    assert t.dtmf.get_nowait() == "7"
    assert await t.receive() is None and t.closed
    t._sender.cancel()


@pytest.mark.asyncio
async def test_transport_close_sends_terminate_packet(monkeypatch):
    from pbx_bridge import asterisk_transport as at

    monkeypatch.setattr(at, "KEEPALIVE_SECONDS", 10)
    conn = FakeConn()
    t = AsteriskTransport(conn)
    await t.close()
    assert conn.sent[-1] == (0x00, b"")


@pytest.mark.asyncio
async def test_placeholder_turn_after_the_goodbye_is_not_spoken():
    filler = audio_msg("<no speech>{pause}")
    client = FakeClient([audio_msg("Goodbye! "), end_call_msg(), filler, turn_complete()])
    line = FakeLine()
    result, ctx = await agent(client, line)
    assert result.reason == "completed"
    assert len(line.sent) == 1  # only the goodbye reached the caller


@pytest.mark.asyncio
async def test_placeholder_text_is_kept_out_of_the_transcript():
    t = TranscriptLog(None, "c1")
    await t.add("ai", "<no speech>{pause}")
    await t.add("ai", "Hello {pause}")
    await t.flush()
    assert t.turns == [{"role": "ai", "text": "Hello"}]


@pytest.mark.asyncio
async def test_menu_key_ends_the_agent_for_the_language_menu():
    client = FakeClient([audio_msg("Hello! ")])
    line = FakeLine()
    line.dtmf.put_nowait("#")
    ctx = make_ctx()
    ctx.language_menu_key = "#"
    result, _ = await agent(client, line, ctx)
    assert result.reason == "language_menu"
    assert line.cleared >= 1
    assert not any("keypad digit" in n for n in client.sessions[0].notes)


@pytest.mark.asyncio
async def test_language_changed_continues_the_call_without_greeting():
    from gemini_session.persona import greeting_note, language_switched_note

    client = FakeClient([audio_msg("Sure. "), turn_complete(), end_call_msg()])
    transcript = TranscriptLog(None, "c1")
    await transcript.add("caller", "Book a manicure")
    await transcript.flush()
    ctx = make_ctx()
    await asyncio.wait_for(run_agent(FakeLine(), ctx, transcript, client, "m", language_changed=True), timeout=10)
    notes = client.sessions[0].notes
    assert language_switched_note("en") in notes
    assert greeting_note(None, "en") not in notes
    assert "Book a manicure" in client.configs[0].system_instruction


@pytest.mark.asyncio
async def test_caller_speech_reaches_the_open_booking_review():
    ctx = make_ctx()
    ctx.review = {"details": {}, "booking_id": "", "reply": "", "unclear": 0}
    client = FakeClient([caller_said("ඔව් හරි"), end_call_msg()])
    await agent(client, ctx=ctx)
    assert ctx.review["reply"] == "ඔව් හරි"


# ---------- a "hello?" the model never answered ----------

def _waiting_silence(monkeypatch) -> tuple["live_session.Silence", list[float]]:
    """A Silence whose agent has just finished asking and waits for the caller; the clock is a list we move."""
    clock = [1000.0]
    monkeypatch.setattr(live_session.time, "monotonic", lambda: clock[0])
    silence = live_session.Silence()
    silence.turn_audio = True
    silence.agent_finished()
    return silence, clock


def test_a_short_sound_the_model_ignored_is_re_asked_after_a_few_seconds(monkeypatch):
    silence, clock = _waiting_silence(monkeypatch)
    clock[0] += 2
    silence.caller_heard()  # the gate heard "hello?" but nothing was transcribed
    assert not silence.should_prompt(8, model_busy=False)  # voice right now: never talk over it
    clock[0] += 2
    assert not silence.should_prompt(8, model_busy=False)  # 2 s: the transcription may still arrive
    clock[0] += 2
    assert silence.should_prompt(8, model_busy=False)  # 4 s later: well before the 8 s rule


def test_a_transcribed_sound_is_not_re_asked(monkeypatch):
    silence, clock = _waiting_silence(monkeypatch)
    clock[0] += 2
    silence.caller_heard()
    clock[0] += 1
    silence.caller_spoke()  # the model heard it
    clock[0] += 5
    assert not silence.should_prompt(8, model_busy=False)


def test_a_sound_while_the_agent_is_mid_turn_is_not_re_asked(monkeypatch):
    silence, clock = _waiting_silence(monkeypatch)
    clock[0] += 2
    silence.caller_heard()
    clock[0] += 5
    assert not silence.should_prompt(8, model_busy=True)


@pytest.mark.asyncio
async def test_goodbye_in_the_same_turn_as_anything_else_does_not_hang_up():
    from tools import registry

    ctx = make_ctx()
    ctx.asked_anything_else = True
    refused = await registry.dispatch("end_call", {"reason": "done"}, ctx)
    assert "wait for their answer" in refused["error"] and not ctx.hangup_requested
    assert (await registry.dispatch("end_call", {"reason": "no_response"}, ctx))["status"] == "call_ending"

    ctx = make_ctx()
    ctx.asked_anything_else = True
    ctx.caller_said("නැහැ ස්තුතියි")  # the caller answered
    assert (await registry.dispatch("end_call", {"reason": "done"}, ctx))["status"] == "call_ending"


@pytest.mark.asyncio
async def test_anything_else_question_in_the_end_call_turn_blocks_the_hang_up():
    spoken = audio_msg("ඔබට වෙනත් යමක් දැනගැනීමට අවශ්‍යද?")
    client = FakeClient([spoken, end_call_msg(), turn_complete()])
    result, ctx = await agent(client, max_seconds=1.5)
    assert not ctx.hangup_requested and result.reason == "time_limit"  # end_call was refused, the call went on
