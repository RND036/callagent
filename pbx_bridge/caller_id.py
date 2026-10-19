"""Caller ID for AudioSocket calls. The AudioSocket protocol itself carries
only a call UUID, so the dialplan registers the caller's number against that
UUID over HTTP (CURL) just before AudioSocket() connects -- see
/api/pbx/call-start in dashboard/server.py.

The language menu ("press 1 for Sinhala...") is played by nailagent itself
(pbx_bridge/ivr.py): upload the recording and set the keys on the dashboard's
"Language Menu (IVR)" tab. The dialplan stays short and sends no lang=:

    [from-ucm-trunk]
    exten => _X.,1,Answer()
     same => n,Set(UUID=${FILTER(0-9a-f\\-,${SHELL(uuidgen -t)})})
     same => n,Set(CURLOPT(httptimeout)=2)
     same => n,Set(REG=${CURL(http://127.0.0.1:8080/api/pbx/call-start?uuid=${UUID}&caller=${URIENCODE(${CALLERID(num)})}&pai=${URIENCODE(${PJSIP_HEADER(read,P-Asserted-Identity)})}&name=${URIENCODE(${CALLERID(name)})})})
     same => n,AudioSocket(${UUID},127.0.0.1:8090)
     same => n,Hangup()

Key presses reach nailagent as AudioSocket DTMF packets, so Asterisk must
forward DTMF (set the trunk's DTMF mode to rfc4733 or info). Check with a test
call: the log shows "DTMF digit: X" when a key is pressed.

An older dialplan that still runs its own Read() menu and sends lang=1/2/3 (or
si/ta/en) keeps working: a language sent that way is used as is and the
agent's menu is skipped.
"""
from __future__ import annotations

import re

# A dialable number: optional +, 6-15 digits (E.164 max), allowing the
# usual separators inside it.
_NUMBER_RE = re.compile(r"\+?\d[\d\s\-().]{4,20}\d")
# The user part of a SIP/tel URI, e.g. "Name" <sip:0781827743@10.0.0.1>
_URI_USER_RE = re.compile(r"(?:sips?|tel):(\+?[\d\-().]+)")


# A dotted IP (the UCM's address) is not a phone number: "192.168.1.160" must not become "1921681160".
# The negative lookarounds keep it from eating part of a longer digit run.
_IPV4_RE = re.compile(r"(?<![\d.])\d{1,3}(?:\.\d{1,3}){3}(?![\d.])")


def _normalize(candidate: str) -> str | None:
    digits = re.sub(r"\D", "", candidate)
    if not 6 <= len(digits) <= 15:
        return None
    return ("+" if candidate.lstrip().startswith("+") else "") + digits


def pick_caller_number(caller: str | None, pai: str | None = None, name: str | None = None) -> str | None:
    """The first real phone number among CALLERID(num), the
    P-Asserted-Identity header, and CALLERID(name) -- the UCM sometimes puts
    its trunk name (e.g. "ucmtrunk") in the From number and the real caller
    elsewhere. None when the caller is withheld/anonymous."""
    caller, pai, name = (_IPV4_RE.sub(" ", v) if v else v for v in (caller, pai, name))
    if caller and (match := _NUMBER_RE.search(caller)):
        if number := _normalize(match.group()):
            return number
    if pai and (match := _URI_USER_RE.search(pai) or _NUMBER_RE.search(pai)):
        if number := _normalize(match.group(match.lastindex or 0)):
            return number
    if name and (match := _NUMBER_RE.search(name)):
        if number := _normalize(match.group()):
            return number
    return None
