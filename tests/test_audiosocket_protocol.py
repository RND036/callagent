import uuid

import pytest

from pbx_bridge.audiosocket_protocol import (
    TYPE_DTMF,
    TYPE_ERROR,
    TYPE_TERMINATE,
    TYPE_UUID,
    AudioSocketAdapter,
    ControlProtocolError,
    DtmfEvent,
    HangupEvent,
    MediaEvent,
    StartEvent,
    UnknownEvent,
)


@pytest.fixture
def adapter() -> AudioSocketAdapter:
    return AudioSocketAdapter()


def test_parse_packet_uuid(adapter: AudioSocketAdapter) -> None:
    call_uuid = uuid.uuid4()
    event = adapter.parse_packet(TYPE_UUID, call_uuid.bytes)
    assert event == StartEvent(
        stream_sid=str(call_uuid),
        call_sid=str(call_uuid),
        other_leg_call_id=None,
        account_id=None,
        caller_id_number=None,
    )


def test_parse_packet_uuid_wrong_length_raises(adapter: AudioSocketAdapter) -> None:
    with pytest.raises(ControlProtocolError):
        adapter.parse_packet(TYPE_UUID, b"\x00" * 10)


def test_parse_packet_audio(adapter: AudioSocketAdapter) -> None:
    event = adapter.parse_packet(0x10, b"\x00\x01\x02\x03")
    assert event == MediaEvent(payload=b"\x00\x01\x02\x03", sample_rate=8000)


def test_parse_packet_audio_16khz(adapter: AudioSocketAdapter) -> None:
    event = adapter.parse_packet(0x12, b"\x00\x01")
    assert event.sample_rate == 16000


def test_parse_packet_audio_logs_on_sample_rate_drift(adapter: AudioSocketAdapter, caplog) -> None:
    with caplog.at_level("WARNING"):
        adapter.parse_packet(0x12, b"\x00\x01", expected_sample_rate=8000)
    assert any("disagrees" in record.message for record in caplog.records)


def test_parse_packet_dtmf(adapter: AudioSocketAdapter) -> None:
    event = adapter.parse_packet(TYPE_DTMF, b"5")
    assert event == DtmfEvent(digit="5")


def test_parse_packet_dtmf_wrong_length_raises(adapter: AudioSocketAdapter) -> None:
    with pytest.raises(ControlProtocolError):
        adapter.parse_packet(TYPE_DTMF, b"")


def test_parse_packet_terminate(adapter: AudioSocketAdapter) -> None:
    event = adapter.parse_packet(TYPE_TERMINATE, b"")
    assert isinstance(event, HangupEvent)


def test_parse_packet_error_returns_unknown(adapter: AudioSocketAdapter) -> None:
    event = adapter.parse_packet(TYPE_ERROR, b"\x01")
    assert isinstance(event, UnknownEvent)
    assert event.msg_type == TYPE_ERROR


def test_parse_packet_unrecognized_type_returns_unknown(adapter: AudioSocketAdapter) -> None:
    event = adapter.parse_packet(0x42, b"whatever")
    assert isinstance(event, UnknownEvent)
    assert event.msg_type == 0x42


def test_build_audio_packet(adapter: AudioSocketAdapter) -> None:
    msg_type, payload = adapter.build_audio_packet(b"\x00\x01", 8000)
    assert (msg_type, payload) == (0x10, b"\x00\x01")


def test_build_audio_packet_unsupported_rate_raises(adapter: AudioSocketAdapter) -> None:
    with pytest.raises(ControlProtocolError):
        adapter.build_audio_packet(b"\x00\x01", 22050)


def test_build_hangup_packet(adapter: AudioSocketAdapter) -> None:
    msg_type, payload = adapter.build_hangup_packet()
    assert (msg_type, payload) == (TYPE_TERMINATE, b"")
