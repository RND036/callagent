"""Central config, loaded from environment (via .env if present)."""
from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.1-flash-live-preview")

WS_HOST = os.environ.get("NAILAGENT_WS_HOST", "127.0.0.1")
WS_PORT = int(os.environ.get("NAILAGENT_WS_PORT", "8766"))

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("NAILAGENT_DATA_DIR", BASE_DIR / "data"))
SEED_SERVICES_PATH = DATA_DIR / "seed_services.json"
BOOKINGS_PATH = DATA_DIR / "bookings.json"
MESSAGES_PATH = DATA_DIR / "messages.json"

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

# FreeSWITCH mod_audio_stream PCM16 sample rate -- must match the
# <sampling-rate> arg in the FusionPBX dialplan's uuid_audio_stream call and
# the metadata string's sample_rate= field.
FS_STREAM_SAMPLE_RATE_HZ = int(os.environ.get("NAILAGENT_FS_SAMPLE_RATE_HZ", "16000"))

LIVE_SESSION_WARN_AFTER_SECONDS = 13 * 60  # ~2 min before the ~15 min Live API cap

# FreeSWITCH mod_audio_stream WebSocket protocol.
START_TIMEOUT_SECONDS = float(os.environ.get("NAILAGENT_START_TIMEOUT_SECONDS", "15"))

# MCP server for call transfer/hangup (separate from the mod_audio_stream
# WebSocket, which has no transfer mechanism of its own). Leave
# MCP_SERVER_URL unset to disable transfer/hangup.
MCP_SERVER_URL = os.environ.get("MCP_SERVER_URL", "")
MCP_API_KEY = os.environ.get("MCP_API_KEY", "")
