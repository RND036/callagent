"""Central config, loaded from environment (via .env if present)."""
from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.1-flash-live-preview")

WS_HOST = os.environ.get("NAILAGENT_WS_HOST", "127.0.0.1")
WS_PORT = int(os.environ.get("NAILAGENT_WS_PORT", "8090"))

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("NAILAGENT_DATA_DIR", BASE_DIR / "data"))
SEED_SERVICES_PATH = DATA_DIR / "seed_services.json"
BOOKINGS_PATH = DATA_DIR / "bookings.json"
MESSAGES_PATH = DATA_DIR / "messages.json"
CALLS_PATH = DATA_DIR / "calls.json"
SETTINGS_PATH = DATA_DIR / "settings.json"

# Dashboard Web UI
DASHBOARD_HOST = os.environ.get("DASHBOARD_HOST", "0.0.0.0")
DASHBOARD_PORT = int(os.environ.get("DASHBOARD_PORT", "8080"))
DASHBOARD_ENABLED = os.environ.get("DASHBOARD_ENABLED", "true").lower() in ("1", "true", "yes")

# Business hours/slot granularity.
BUSINESS_HOURS_START = "09:00"
BUSINESS_HOURS_END = "19:00"  # 7:00 PM
SLOT_INCREMENT_MINUTES = 30

SPA_NAME = "CLASSY NAIL SPA"
SPA_ADDRESS = "309 Kotte Road, Nugegoda 10250, Sri Lanka"
SPA_PHONE = "+94 77 741 2853"
SPA_HOURS = "Daily from 9:00 AM to 7:00 PM"

# Telephony audio contract vs. Gemini Live API.
GEMINI_INPUT_SAMPLE_RATE_HZ = 16000
GEMINI_OUTPUT_SAMPLE_RATE_HZ = 24000

# Asterisk AudioSocket PCM16 sample rate. AudioSocket's packet type byte
# selects the rate (0x10=8kHz ... 0x18=192kHz), but a PSTN/SIP trunk (the
# UCM6302A here) realistically negotiates 8kHz codecs, so this is real audio
# resampling up to GEMINI_INPUT_SAMPLE_RATE_HZ, not a passthrough. Confirm
# against a real call if this ever changes.
AUDIOSOCKET_SAMPLE_RATE_HZ = int(os.environ.get("NAILAGENT_AUDIOSOCKET_SAMPLE_RATE_HZ", "8000"))

LIVE_SESSION_WARN_AFTER_SECONDS = 13 * 60  # ~2 min before the ~15 min Live API cap

# Asterisk AudioSocket protocol (raw TCP, TLV-framed) -- see pbx_bridge/audiosocket_protocol.py.
START_TIMEOUT_SECONDS = float(os.environ.get("NAILAGENT_START_TIMEOUT_SECONDS", "15"))

# MCP server for call transfer (AudioSocket has no transfer primitive of its
# own; hangup can go over the socket directly, see build_hangup_packet).
# Leave MCP_SERVER_URL unset to disable transfer.
MCP_SERVER_URL = os.environ.get("MCP_SERVER_URL", "")
MCP_API_KEY = os.environ.get("MCP_API_KEY", "")
