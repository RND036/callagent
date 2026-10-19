"""Central config, loaded from environment (via .env if present)."""
from __future__ import annotations

import os
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

load_dotenv()

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.1-flash-live-preview")
# The Live model generates the voice itself, so timbre can drift between turns
# (mostly in Sinhala/Tamil). If it does, try a lower GEMINI_TEMPERATURE (e.g.
# 0.7) or another prebuilt voice (e.g. Kore). Temperature is only sent if set.
GEMINI_VOICE_NAME = os.environ.get("GEMINI_VOICE_NAME", "Aoede")
GEMINI_TEMPERATURE = float(os.environ["GEMINI_TEMPERATURE"]) if os.environ.get("GEMINI_TEMPERATURE") else None

# The salon's clock: "today"/"tomorrow" in the agent's prompt and the
# past-date checks on bookings are computed in this timezone.
SALON_TZ = ZoneInfo(os.environ.get("NAILAGENT_TIMEZONE", "Asia/Colombo"))

WS_HOST = os.environ.get("NAILAGENT_WS_HOST", "127.0.0.1")
WS_PORT = int(os.environ.get("NAILAGENT_WS_PORT", "8090"))

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("NAILAGENT_DATA_DIR", BASE_DIR / "data"))
SEED_SERVICES_PATH = DATA_DIR / "seed_services.json"
BOOKINGS_PATH = DATA_DIR / "bookings.json"
MESSAGES_PATH = DATA_DIR / "messages.json"
CALLS_PATH = DATA_DIR / "calls.json"
# Calls in progress, rewritten as each call goes on, so a crash or restart mid-call
# still leaves its transcript and customer details (recovered on the next start).
ACTIVE_CALLS_PATH = DATA_DIR / "active_calls.json"
SETTINGS_PATH = DATA_DIR / "settings.json"
FAQS_PATH = DATA_DIR / "faqs.json"
# Language-menu (IVR) settings and the recordings uploaded from the dashboard.
IVR_PATH = DATA_DIR / "ivr.json"
IVR_PROMPTS_DIR = DATA_DIR / "ivr_prompts"
# When set, the uploaded menu recording is also copied here (e.g. /var/lib/asterisk/sounds/nailagent/lang-menu.wav)
# so an Asterisk dialplan Read() menu can play it -- Asterisk 20's AudioSocket does not pass key presses to us.
IVR_ASTERISK_MENU_PATH = os.environ.get("NAILAGENT_ASTERISK_MENU_PATH", "")
RECORDINGS_DIR = DATA_DIR / "recordings"
# Record both sides of each phone call to a WAV shown in Call History.
RECORDINGS_ENABLED = os.environ.get("NAILAGENT_RECORDINGS_ENABLED", "true").lower() in ("1", "true", "yes")

# Dashboard Web UI
DASHBOARD_HOST = os.environ.get("DASHBOARD_HOST", "0.0.0.0")
DASHBOARD_PORT = int(os.environ.get("DASHBOARD_PORT", "8080"))
DASHBOARD_ENABLED = os.environ.get("DASHBOARD_ENABLED", "true").lower() in ("1", "true", "yes")
DASHBOARD_USERNAME = os.environ.get("DASHBOARD_USERNAME", "admin")
DASHBOARD_PASSWORD = os.environ.get("DASHBOARD_PASSWORD", "admin")

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

# Near-field noise gate on the caller's audio (audio/noise_gate.py): only
# audio clearly louder than the background -- the person speaking into the
# handset -- reaches Gemini; TV, traffic and people talking nearby are sent as
# silence. Levels are PCM16 RMS at AUDIOSOCKET_SAMPLE_RATE_HZ (normal phone
# speech is roughly 1000-4000). Each call logs a "Noise gate" summary line with
# its measured background and loudest levels; use it to tune these. Raise
# MIN_RMS / OPEN_RATIO if background still gets through, lower them if quiet
# callers get cut off.
NOISE_GATE_ENABLED = os.environ.get("NAILAGENT_NOISE_GATE_ENABLED", "true").lower() in ("1", "true", "yes")
NOISE_GATE_MIN_RMS = int(os.environ.get("NAILAGENT_NOISE_GATE_MIN_RMS", "400"))
NOISE_GATE_OPEN_RATIO = float(os.environ.get("NAILAGENT_NOISE_GATE_OPEN_RATIO", "3.0"))
NOISE_GATE_ATTACK_MS = float(os.environ.get("NAILAGENT_NOISE_GATE_ATTACK_MS", "40"))
NOISE_GATE_HANGOVER_MS = float(os.environ.get("NAILAGENT_NOISE_GATE_HANGOVER_MS", "500"))
NOISE_GATE_PREROLL_MS = float(os.environ.get("NAILAGENT_NOISE_GATE_PREROLL_MS", "300"))

# Hard cap on one call, and how Live "notes" (greeting cue, still-there prompt)
# are sent: client_content (default) or realtime_text if the model rejects them.
MAX_CALL_SECONDS = float(os.environ.get("NAILAGENT_MAX_CALL_SECONDS", "900"))
# How long one Gemini outage may last while the caller waits on the line: the agent
# keeps reconnecting (with backoff) until then, and only gives up after it.
RECONNECT_WINDOW_SECONDS = float(os.environ.get("NAILAGENT_RECONNECT_WINDOW_SECONDS", "20"))
LIVE_NOTE_MODE = os.environ.get("NAILAGENT_LIVE_NOTE_MODE", "client_content")

# Gemini turn-taking (voice activity detection).
VAD_START_SENSITIVITY = os.environ.get("NAILAGENT_VAD_START_SENSITIVITY", "low").lower()
VAD_END_SENSITIVITY = os.environ.get("NAILAGENT_VAD_END_SENSITIVITY", "low").lower()
VAD_SILENCE_MS = int(os.environ.get("NAILAGENT_VAD_SILENCE_MS", "1000"))
VAD_PREFIX_PADDING_MS = int(os.environ.get("NAILAGENT_VAD_PREFIX_PADDING_MS", "300"))

# Silence check-in: if the caller goes quiet after the AI's turn ends, nudge
# once ("are you still there?"), warn once more, then end the call if there's
# still no reply.
SILENCE_CHECKIN_TIMEOUT_SECONDS = float(os.environ.get("NAILAGENT_SILENCE_CHECKIN_TIMEOUT_SECONDS", "8"))
SILENCE_CHECKINS_BEFORE_HANGUP = 2

# Asterisk AudioSocket protocol (raw TCP, TLV-framed) -- see pbx_bridge/audiosocket_protocol.py.
START_TIMEOUT_SECONDS = float(os.environ.get("NAILAGENT_START_TIMEOUT_SECONDS", "15"))

# MCP server for call transfer (AudioSocket has no transfer primitive of its
# own; hangup can go over the socket directly, see build_hangup_packet).
# Leave MCP_SERVER_URL unset to disable transfer.
MCP_SERVER_URL = os.environ.get("MCP_SERVER_URL", "")
MCP_API_KEY = os.environ.get("MCP_API_KEY", "")
