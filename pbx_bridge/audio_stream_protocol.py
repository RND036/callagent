"""Adapter for FreeSWITCH's mod_audio_stream WebSocket protocol
(https://github.com/amigniter/mod_audio_stream).

Unlike SmartPBX's JSON-only protocol, mod_audio_stream's inbound traffic is
two structurally different kinds of message, not one dispatched-by-field
envelope:

  1. One freeform metadata TEXT frame, sent once at stream start (the last
     arg of the dialplan's `uuid_audio_stream <uuid> start <wss-url>
     <mix-type> <sampling-rate> <metadata>` call). We use a `key=value;...`
     format (not JSON) since it needs no escaping inside a dialplan XML
     `data="..."` attribute.
  2. A continuous stream of raw binary L16 PCM frames (no envelope at all --
     the whole WS binary payload is audio).

The module can also send rare JSON TEXT control messages
(`mod_audio_stream::connect` / `::disconnect` / `::error` / `::json`), which
`parse_text_message` handles.

Outbound (server -> FreeSWITCH), used only if the caller's mod_audio_stream
build supports bidirectional playback:

    {"type": "streamAudio", "data": {"audioDataType": "raw",
     "sampleRate": ..., "audioData": "<base64 PCM16>"}}

This is deliberately sent as WS TEXT even though inbound audio is binary --
that in/out asymmetry is correct for this protocol, not a bug.
"""
from __future__ import annotations

import base64
import json
import logging
from dataclasses import dataclass
from typing import Union

logger = logging.getLogger(__name__)


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
    sample_rate: int = 16000


@dataclass(frozen=True)
class MediaEvent:
    payload: bytes  # raw PCM16 audio, already decoded (no base64/mu-law step)


@dataclass(frozen=True)
class HangupEvent:
    reason: str | None = None


@dataclass(frozen=True)
class UnknownEvent:
    raw: dict


ControlEvent = Union[StartEvent, MediaEvent, HangupEvent, UnknownEvent]

_DISCONNECT_TYPES = {"mod_audio_stream::disconnect", "mod_audio_stream::error"}


def _parse_metadata_string(raw: str) -> dict[str, str]:
    fields: dict[str, str] = {}
    for pair in raw.split(";"):
        pair = pair.strip()
        if not pair:
            continue
        key, sep, value = pair.partition("=")
        if not sep:
            continue
        fields[key.strip()] = value.strip()
    return fields


class FsAudioStreamAdapter:
    def parse_stream_metadata(self, raw: str, expected_sample_rate: int | None = None) -> StartEvent:
        if not isinstance(raw, str) or not raw.strip():
            raise ControlProtocolError(f"Stream metadata must be a non-empty string, got: {raw!r}")

        fields = _parse_metadata_string(raw)
        call_uuid = fields.get("call_uuid")
        if not call_uuid:
            raise ControlProtocolError(f"Stream metadata missing call_uuid: {raw!r}")

        sample_rate_str = fields.get("sample_rate")
        sample_rate = int(sample_rate_str) if sample_rate_str else 16000
        if expected_sample_rate is not None and sample_rate != expected_sample_rate:
            logger.warning(
                "Stream metadata sample_rate=%s disagrees with configured %s (dialplan/config drift?)",
                sample_rate,
                expected_sample_rate,
            )

        return StartEvent(
            stream_sid=call_uuid,
            call_sid=call_uuid,
            other_leg_call_id=None,
            account_id=fields.get("destination_number"),
            caller_id_number=fields.get("caller_id_number"),
            sample_rate=sample_rate,
        )

    def wrap_audio_frame(self, pcm_bytes: bytes) -> MediaEvent:
        return MediaEvent(payload=pcm_bytes)

    def parse_text_message(self, raw: str) -> ControlEvent:
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ControlProtocolError(f"Malformed control message: {exc}") from exc
        if not isinstance(data, dict):
            raise ControlProtocolError(f"Control message must be a JSON object, got: {data!r}")

        msg_type = data.get("type")
        if msg_type in _DISCONNECT_TYPES:
            return HangupEvent(reason=msg_type)
        return UnknownEvent(raw=data)

    def build_stream_audio_frame(self, pcm_bytes: bytes, sample_rate: int) -> dict:
        return {
            "type": "streamAudio",
            "data": {
                "audioDataType": "raw",
                "sampleRate": sample_rate,
                "audioData": base64.b64encode(pcm_bytes).decode("ascii"),
            },
        }
