import base64

import pytest

from pbx_bridge.control_protocol import (
    ControlProtocolError,
    DtmfEvent,
    HangupEvent,
    MediaEvent,
    PBXControlAdapter,
    StartEvent,
    UnknownEvent,
)


@pytest.fixture
def adapter() -> PBXControlAdapter:
    return PBXControlAdapter()


def test_parse_start(adapter: PBXControlAdapter) -> None:
    event = adapter.parse(
        {
            "event": "start",
            "start": {
                "streamSid": "stream-1",
                "callSid": "call-1",
                "otherLegCallId": "leg-2",
                "accountId": "acct-1",
                "callerIdNumber": "+94771234567",
                "mediaFormat": {"encoding": "g711_ulaw", "sampleRate": 8000},
            },
        }
    )
    assert event == StartEvent(
        stream_sid="stream-1",
        call_sid="call-1",
        other_leg_call_id="leg-2",
        account_id="acct-1",
        caller_id_number="+94771234567",
        encoding="g711_ulaw",
        sample_rate=8000,
    )


def test_parse_start_missing_start_object_raises(adapter: PBXControlAdapter) -> None:
    with pytest.raises(ControlProtocolError):
        adapter.parse({"event": "start"})


def test_parse_start_with_call_id(adapter: PBXControlAdapter) -> None:
    event = adapter.parse({"event": "start", "start": {"callId": "call-1", "callerIdNumber": "0781827743"}})
    assert event.stream_sid == "call-1"
    assert event.call_sid == "call-1"
    assert event.caller_id_number == "0781827743"


def test_parse_media(adapter: PBXControlAdapter) -> None:
    payload = base64.b64encode(b"\x00\x01\x02").decode()
    event = adapter.parse({"event": "media", "media": {"payload": payload}})
    assert event == MediaEvent(payload=b"\x00\x01\x02")


def test_parse_dtmf_under_dtmf_key(adapter: PBXControlAdapter) -> None:
    event = adapter.parse({"event": "dtmf", "dtmf": {"digit": "5"}})
    assert event == DtmfEvent(digit="5")


def test_parse_dtmf_under_media_key(adapter: PBXControlAdapter) -> None:
    # Spec is self-contradictory about where the digit lives; this shape
    # (digit nested under "media" with a "media" event name) must also work.
    event = adapter.parse({"event": "media", "media": {"digit": "7"}})
    assert event == DtmfEvent(digit="7")


@pytest.mark.parametrize("event_name", ["hangup", "stop", "close", "end"])
def test_parse_hangup_variants(adapter: PBXControlAdapter, event_name: str) -> None:
    event = adapter.parse({"event": event_name})
    assert isinstance(event, HangupEvent)


def test_parse_unknown_event_returned_not_raised(adapter: PBXControlAdapter) -> None:
    event = adapter.parse({"event": "something_new", "foo": "bar"})
    assert event == UnknownEvent(raw={"event": "something_new", "foo": "bar"})


def test_parse_non_dict_raises(adapter: PBXControlAdapter) -> None:
    with pytest.raises(ControlProtocolError):
        adapter.parse("not a dict")  # type: ignore[arg-type]


def test_build_media_frame(adapter: PBXControlAdapter) -> None:
    frame = adapter.build_media_frame("stream-1", b"\x00\x01")
    assert frame == {
        "event": "media",
        "streamSid": "stream-1",
        "media": {"payload": base64.b64encode(b"\x00\x01").decode()},
    }


@pytest.mark.asyncio
async def test_paced_media_sender_framing_and_flush(adapter: PBXControlAdapter) -> None:
    from unittest.mock import AsyncMock
    from pbx_bridge.call_session import PacedMediaSender

    mock_ws = AsyncMock()
    sender = PacedMediaSender(mock_ws, adapter, "test-stream")

    # Enqueue 200 bytes: 160 should be enqueued, 40 retained in remainder
    sender.enqueue(b"\xaa" * 200)
    assert sender._queue.qsize() == 1
    frame1 = sender._queue.get_nowait()
    assert len(frame1) == 160
    assert frame1 == b"\xaa" * 160
    assert len(sender._remainder) == 40

    # Flush should pad the 40 bytes with 120 bytes of 0xFF (silence) to make a 160-byte frame
    sender.flush()
    assert sender._queue.qsize() == 1
    flushed_frame = sender._queue.get_nowait()
    assert len(flushed_frame) == 160
    assert flushed_frame == (b"\xaa" * 40) + (b"\xff" * 120)
    assert len(sender._remainder) == 0

    # Barge in clears both queue and remainder
    sender.enqueue(b"\xbb" * 100)
    sender.barge_in()
    assert len(sender._remainder) == 0
    assert sender._queue.qsize() == 0

    await sender.close()
