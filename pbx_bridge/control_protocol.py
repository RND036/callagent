"""Adapter for SmartPBX's Dialog WebSocket control channel.

Wire format (JSON TEXT frames only -- SmartPBX has no WS binary audio frames;
even media is base64-encoded inside JSON):

    {"event": "start", "start": {
        "streamSid": "...", "callSid": "...", "otherLegCallId": "...",
        "accountId": "...", "callerIdNumber": "+94...",
        "mediaFormat": {"encoding": "g711_ulaw", "sampleRate": 8000}
    }}
    {"event": "media", "media": {"payload": "<base64 ulaw>"}}   -- ~50/s
    {"event": "dtmf", ...}                                       -- see _extract_dtmf_digit
    {"event": "hangup" | "stop" | "close" | "end", ...}          -- "hangup" observed in prod

Outbound (server -> SmartPBX):
    {"event": "media", "streamSid": "...", "media": {"payload": "<base64 ulaw>"}}

Section 1.3.5 of the spec only defines start/media/dtmf/hangup -- there is no
flush/clear/cancel event. Once audio is sent SmartPBX will play it out; it
cannot be recalled (see call_session.py's barge-in handling).
"""
from __future__ import annotations

import base64
from dataclasses import dataclass
from typing import Union


class ControlProtocolError(Exception):
    pass


@dataclass(frozen=True)
class StartEvent:
    stream_sid: str
    call_sid: str | None
    other_leg_call_id: str | None
    account_id: str | None
    caller_id_number: str | None
    encoding: str = "g711_ulaw"
    sample_rate: int = 8000


@dataclass(frozen=True)
class MediaEvent:
    payload: bytes  # decoded mu-law audio


@dataclass(frozen=True)
class DtmfEvent:
    digit: str


@dataclass(frozen=True)
class HangupEvent:
    reason: str | None = None


@dataclass(frozen=True)
class UnknownEvent:
    raw: dict


ControlEvent = Union[StartEvent, MediaEvent, DtmfEvent, HangupEvent, UnknownEvent]

_HANGUP_EVENT_NAMES = {"hangup", "stop", "close", "end", "disconnect"}


def _extract_dtmf_digit(raw: dict) -> str | None:
    # Spec section 1.3.3 says the digit rides under "media"; the worked
    # example in the same doc puts it under "dtmf". SmartPBX has been
    # observed doing either, so check both rather than trusting one shape.
    dtmf = raw.get("dtmf")
    if isinstance(dtmf, dict) and dtmf.get("digit"):
        return str(dtmf["digit"])
    media = raw.get("media")
    if isinstance(media, dict) and media.get("digit"):
        return str(media["digit"])
    return None


class PBXControlAdapter:
    def parse(self, raw: dict) -> ControlEvent:
        if not isinstance(raw, dict):
            raise ControlProtocolError(f"Control message must be a JSON object, got: {raw!r}")

        event_name = raw.get("event")

        if event_name == "start":
            start = raw.get("start")
            if not isinstance(start, dict):
                raise ControlProtocolError(f"'start' event missing 'start' object: {raw!r}")
            stream_sid = (
                start.get("streamSid")
                or raw.get("streamSid")
                or start.get("callId")
                or raw.get("callId")
                or start.get("callSid")
                or raw.get("callSid")
                or "default-stream"
            )
            call_sid = (
                start.get("callSid")
                or raw.get("callSid")
                or start.get("callId")
                or raw.get("callId")
            )
            media_format = start.get("mediaFormat") or {}
            return StartEvent(
                stream_sid=str(stream_sid),
                call_sid=str(call_sid) if call_sid else None,
                other_leg_call_id=start.get("otherLegCallId") or raw.get("otherLegCallId"),
                account_id=start.get("accountId") or raw.get("accountId"),
                caller_id_number=start.get("callerIdNumber") or start.get("from") or raw.get("callerIdNumber"),
                encoding=media_format.get("encoding", "g711_ulaw"),
                sample_rate=media_format.get("sampleRate", 8000),
            )

        if event_name == "media":
            media = raw.get("media") or {}
            payload_b64 = media.get("payload")
            if payload_b64 is None:
                digit = _extract_dtmf_digit(raw)
                if digit:
                    return DtmfEvent(digit=digit)
                raise ControlProtocolError(f"'media' event missing payload: {raw!r}")
            try:
                payload = base64.b64decode(payload_b64)
            except Exception as exc:  # noqa: BLE001
                raise ControlProtocolError(f"Invalid base64 media payload: {exc}") from exc
            return MediaEvent(payload=payload)

        if event_name == "dtmf":
            digit = _extract_dtmf_digit(raw)
            if not digit:
                raise ControlProtocolError(f"'dtmf' event missing digit: {raw!r}")
            return DtmfEvent(digit=digit)

        if event_name in _HANGUP_EVENT_NAMES:
            return HangupEvent(reason=raw.get("reason") or event_name)

        return UnknownEvent(raw=raw)

    def build_media_frame(self, stream_sid: str, ulaw_payload: bytes) -> dict:
        return {
            "event": "media",
            "streamSid": stream_sid,
            "media": {"payload": base64.b64encode(ulaw_payload).decode("ascii")},
        }

    def build_transfer_frame(self, stream_sid: str, target: str) -> dict:
        # WS-level fallback (undocumented, best-effort) -- only used if the
        # MCP transfer call itself fails.
        return {"event": "transfer", "streamSid": stream_sid, "transfer": {"target": target}}
