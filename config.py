"""Central config. Loads .env once and exposes typed settings."""
from __future__ import annotations

import os
from dotenv import load_dotenv

load_dotenv()


def _required(key: str) -> str:
    val = os.getenv(key, "").strip()
    if not val:
        raise RuntimeError(
            f"Missing required config: {key}. "
            f"Copy .env.example to .env and fill it in."
        )
    return val


# --- Telegram ---
TELEGRAM_BOT_TOKEN = _required("TELEGRAM_BOT_TOKEN")

# --- OpenAI ---
OPENAI_API_KEY = _required("OPENAI_API_KEY")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini").strip()

# --- Voice (speech-to-text + text-to-speech) ---
STT_MODEL = os.getenv("STT_MODEL", "whisper-1").strip()        # transcription
# Force a transcription language (e.g. "hi" or "en"); empty = auto-detect.
STT_LANGUAGE = os.getenv("STT_LANGUAGE", "").strip()
TTS_MODEL = os.getenv("TTS_MODEL", "tts-1").strip()            # spoken replies
TTS_VOICE = os.getenv("TTS_VOICE", "nova").strip()             # nova/shimmer/alloy/...
# Reply with a spoken voice note when the user sends a voice note.
VOICE_REPLIES = os.getenv("VOICE_REPLIES", "true").strip().lower() != "false"

# --- Security ---
ENCRYPTION_KEY = _required("ENCRYPTION_KEY").encode()

# Whitelist: these Telegram IDs are auto-approved owners (never asked to verify).
ALLOWED_TELEGRAM_IDS = {
    int(x) for x in os.getenv("ALLOWED_TELEGRAM_IDS", "").split(",") if x.strip()
}

# Passphrase gate: anyone else must answer this question correctly to get access.
GODFATHER_QUESTION = os.getenv(
    "GODFATHER_QUESTION", "🔒 Enter the secret access code to use this bot:"
).strip()
GODFATHER_ANSWER = os.getenv("GODFATHER_ANSWER", "Ankur Singh").strip()

# Brute-force protection: after this many wrong codes, ban for BAN_HOURS.
MAX_CODE_ATTEMPTS = int(os.getenv("MAX_CODE_ATTEMPTS", "5"))
BAN_HOURS = int(os.getenv("BAN_HOURS", "24"))

# --- Google OAuth (used from Phase 2 on) ---
GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID", "").strip()
GOOGLE_CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET", "").strip()
OAUTH_REDIRECT_URI = os.getenv(
    "OAUTH_REDIRECT_URI", "http://localhost:8000/oauth/callback"
).strip()

# Port the OAuth callback web server listens on (Coolify/hosts may set PORT).
PORT = int(os.getenv("PORT", "8000"))

# Google permissions we ask each user for. Least-privilege on purpose:
#  - gmail.readonly : read statements/transaction mails (cannot delete/send as you)
#  - gmail.send     : send you notification mails
#  - spreadsheets   : maintain your sheets
#  - drive.file     : ONLY files the app itself creates (cannot see your other Drive files)
# OAuth (personal Google login) — a linked account can access EVERYTHING:
# Gmail, Calendar, Docs, Drive, Sheets. (Code still never deletes; read+write only.)
GOOGLE_SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",       # read emails/statements
    "https://www.googleapis.com/auth/gmail.send",           # send mail
    "https://www.googleapis.com/auth/calendar.events",      # calendar
    "https://www.googleapis.com/auth/documents",            # docs
    "https://www.googleapis.com/auth/drive",                # drive (read/write; no delete in code)
    "https://www.googleapis.com/auth/spreadsheets",         # sheets
    "openid",
    "https://www.googleapis.com/auth/userinfo.email",
]

# --- Google Service Account (share-a-sheet model) ---
# Path to the service-account JSON key. Users share their Sheet/Drive folder with
# this account's email, then send the bot the link — no per-user login needed.
GOOGLE_SERVICE_ACCOUNT_FILE = os.getenv(
    "GOOGLE_SERVICE_ACCOUNT_FILE", "service_account.json"
).strip()

# --- Storage ---
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///brain.db").strip()

# --- Scheduler (Phase 4) ---
# Timezone for all scheduled jobs. India by default.
TIMEZONE = os.getenv("TIMEZONE", "Asia/Kolkata").strip()
# Hour (0-23, local time) the daily job runs to check statements + due dates.
DAILY_JOB_HOUR = int(os.getenv("DAILY_JOB_HOUR", "9"))
# How many days before a bill's due date to send a reminder.
DUE_REMINDER_DAYS = int(os.getenv("DUE_REMINDER_DAYS", "3"))
