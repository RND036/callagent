import base64

import pytest

from pbx_bridge.audio_stream_protocol import (
    ControlProtocolError,
    FsAudioStreamAdapter,
    HangupEvent,
    StartEvent,
    UnknownEvent,
)


@pytest.fixture
def adapter() -> FsAudioStreamAdapter:
    return FsAudioStreamAdapter()


def test_parse_stream_metadata(adapter: FsAudioStreamAdapter) -> None:
    raw = "call_uuid=abc-123;caller_id_number=+94771234567;destination_number=1001;sample_rate=16000"
    event = adapter.parse_stream_metadata(raw)
    assert event == StartEvent(
        stream_sid="abc-123",
        call_sid="abc-123",
        other_leg_call_id=None,
        account_id="1001",
        caller_id_number="+94771234567",
        sample_rate=16000,
    )


def test_parse_stream_metadata_missing_optional_fields(adapter: FsAudioStreamAdapter) -> None:
    event = adapter.parse_stream_metadata("call_uuid=abc-123")
    assert event.stream_sid == "abc-123"
    assert event.account_id is None
    assert event.caller_id_number is None
    assert event.sample_rate == 16000  # default when sample_rate= is absent


def test_parse_stream_metadata_ignores_unknown_keys(adapter: FsAudioStreamAdapter) -> None:
    event = adapter.parse_stream_metadata("call_uuid=abc-123;some_future_field=xyz")
    assert event.stream_sid == "abc-123"


def test_parse_stream_metadata_missing_call_uuid_raises(adapter: FsAudioStreamAdapter) -> None:
    with pytest.raises(ControlProtocolError):
        adapter.parse_stream_metadata("caller_id_number=+94771234567")


def test_parse_stream_metadata_empty_string_raises(adapter: FsAudioStreamAdapter) -> None:
    with pytest.raises(ControlProtocolError):
        adapter.parse_stream_metadata("")


def test_parse_stream_metadata_logs_on_sample_rate_drift(adapter: FsAudioStreamAdapter, caplog) -> None:
    with caplog.at_level("WARNING"):
        adapter.parse_stream_metadata("call_uuid=abc-123;sample_rate=8000", expected_sample_rate=16000)
    assert any("disagrees" in record.message for record in caplog.records)


def test_wrap_audio_frame(adapter: FsAudioStreamAdapter) -> None:
    event = adapter.wrap_audio_frame(b"\x00\x01\x02\x03")
    assert event.payload == b"\x00\x01\x02\x03"


@pytest.mark.parametrize(
    "msg_type", ["mod_audio_stream::disconnect", "mod_audio_stream::error"]
)
def test_parse_text_message_disconnect_and_error(adapter: FsAudioStreamAdapter, msg_type: str) -> None:
    event = adapter.parse_text_message('{"type": "%s"}' % msg_type)
    assert isinstance(event, HangupEvent)
    assert event.reason == msg_type


def test_parse_text_message_unknown_type(adapter: FsAudioStreamAdapter) -> None:
    event = adapter.parse_text_message('{"type": "mod_audio_stream::json", "data": {"foo": "bar"}}')
    assert event == UnknownEvent(raw={"type": "mod_audio_stream::json", "data": {"foo": "bar"}})


def test_parse_text_message_malformed_json_raises(adapter: FsAudioStreamAdapter) -> None:
    with pytest.raises(ControlProtocolError):
        adapter.parse_text_message("not json")


def test_parse_text_message_non_object_raises(adapter: FsAudioStreamAdapter) -> None:
    with pytest.raises(ControlProtocolError):
        adapter.parse_text_message("[1, 2, 3]")


def test_build_stream_audio_frame(adapter: FsAudioStreamAdapter) -> None:
    frame = adapter.build_stream_audio_frame(b"\x00\x01", 16000)
    assert frame == {
        "type": "streamAudio",
        "data": {
            "audioDataType": "raw",
            "sampleRate": 16000,
            "audioData": base64.b64encode(b"\x00\x01").decode("ascii"),
        },
    }
