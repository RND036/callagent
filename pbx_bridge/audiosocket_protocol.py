"""Adapter for Asterisk's AudioSocket protocol
(https://docs.asterisk.org/Configuration/Channel-Drivers/AudioSocket/).

Unlike FreeSWITCH's mod_audio_stream (WebSocket, text-vs-binary framing),
AudioSocket is a raw TCP connection carrying a single stream of TLV
(type-length-value) packets: a 1-byte type, a 2-byte big-endian length, then
the payload. One packet always equals one typed message, so there is a
single dispatcher (parse_packet) rather than mod_audio_stream's split
between a one-time text frame and a continuous binary stream.

Packet types:
    0x00  terminate/hangup (zero-length payload, either direction)
    0x01  UUID (16-byte binary), sent once at connection start
    0x03  DTMF digit (1 ASCII byte) -- native, unlike mod_audio_stream which
          forwards no DTMF at all
    0x10-0x18  PCM audio, signed linear 16-bit mono little-endian; the type
          byte selects the sample rate (0x10=8kHz, 0x11=12kHz, 0x12=16kHz,
          0x13=24kHz, 0x14=32kHz, 0x15=44.1kHz, 0x16=48kHz, 0x17=96kHz,
          0x18=192kHz)
    0xff  error, payload is an application-specific error code

Outbound (server -> Asterisk) audio uses the same TLV framing as inbound --
unlike mod_audio_stream's binary-in/JSON-text-out asymmetry, AudioSocket is
symmetric: no base64, no JSON, in either direction.
"""
from __future__ import annotations

import logging
import uuid as uuid_module
from dataclasses import dataclass
from typing import Union

logger = logging.getLogger(__name__)

TYPE_TERMINATE = 0x00
TYPE_UUID = 0x01
TYPE_DTMF = 0x03
TYPE_ERROR = 0xFF

# Packet type byte -> sample rate (Hz), and its inverse.
_TYPE_TO_SAMPLE_RATE = {
    0x10: 8000,
    0x11: 12000,
    0x12: 16000,
    0x13: 24000,
    0x14: 32000,
    0x15: 44100,
    0x16: 48000,
    0x17: 96000,
    0x18: 192000,
}
_SAMPLE_RATE_TO_TYPE = {rate: msg_type for msg_type, rate in _TYPE_TO_SAMPLE_RATE.items()}


class ControlProtocolError(Exception):
    pass


@dataclass(frozen=True)
class StartEvent:
    stream_sid: str
    call_sid: str | None
    other_leg_call_id: str | None
    account_id: str | None
    caller_id_number: str | None
    encoding: str = "l16"
    sample_rate: int = 8000


@dataclass(frozen=True)
class MediaEvent:
    payload: bytes  # raw PCM16 audio
    sample_rate: int


@dataclass(frozen=True)
class DtmfEvent:
    digit: str


@dataclass(frozen=True)
class HangupEvent:
    reason: str | None = None


@dataclass(frozen=True)
class UnknownEvent:
    raw: bytes
    msg_type: int


ControlEvent = Union[StartEvent, MediaEvent, DtmfEvent, HangupEvent, UnknownEvent]


class AudioSocketAdapter:
    def parse_packet(
        self, msg_type: int, payload: bytes, expected_sample_rate: int | None = None
    ) -> ControlEvent:
        if msg_type == TYPE_UUID:
            if len(payload) != 16:
                raise ControlProtocolError(f"UUID packet must be 16 bytes, got {len(payload)}")
            call_uuid = str(uuid_module.UUID(bytes=payload))
            return StartEvent(
                stream_sid=call_uuid,
                call_sid=call_uuid,
                other_leg_call_id=None,
                account_id=None,
                caller_id_number=None,
            )

        if msg_type in _TYPE_TO_SAMPLE_RATE:
            sample_rate = _TYPE_TO_SAMPLE_RATE[msg_type]
            if expected_sample_rate is not None and sample_rate != expected_sample_rate:
                logger.warning(
                    "Audio packet sample_rate=%s disagrees with configured %s (dialplan/config drift?)",
                    sample_rate,
                    expected_sample_rate,
                )
            return MediaEvent(payload=payload, sample_rate=sample_rate)

        if msg_type == TYPE_DTMF:
            if len(payload) != 1:
                raise ControlProtocolError(f"DTMF packet must be 1 byte, got {len(payload)}")
            return DtmfEvent(digit=payload.decode("ascii"))

        if msg_type == TYPE_TERMINATE:
            return HangupEvent(reason="terminate")

        if msg_type == TYPE_ERROR:
            logger.warning("AudioSocket error packet: %r", payload)
            return UnknownEvent(raw=payload, msg_type=msg_type)

        return UnknownEvent(raw=payload, msg_type=msg_type)

    def build_audio_packet(self, pcm_bytes: bytes, sample_rate: int) -> tuple[int, bytes]:
        msg_type = _SAMPLE_RATE_TO_TYPE.get(sample_rate)
        if msg_type is None:
            raise ControlProtocolError(f"Unsupported AudioSocket sample rate: {sample_rate}")
        return msg_type, pcm_bytes

    def build_hangup_packet(self) -> tuple[int, bytes]:
        return TYPE_TERMINATE, b""
