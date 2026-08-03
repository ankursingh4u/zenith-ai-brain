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
# --- LLM provider (any OpenAI-compatible endpoint) ---
# Leave LLM_BASE_URL empty to use OpenAI directly; set it to a gateway's /v1 to
# route chat + vision elsewhere. Speech (whisper/tts) always stays on OpenAI.
LLM_BASE_URL = os.getenv("LLM_BASE_URL", "").strip()
LLM_API_KEY = os.getenv("LLM_API_KEY", "").strip() or OPENAI_API_KEY

# The reasoning brain — conversation, planning, code, advice. The single
# biggest lever on how smart the bot feels.
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o").strip()
# Tried in order if OPENAI_MODEL isn't available, so a name this key can't use
# degrades instead of breaking every reply.
MODEL_FALLBACKS = [m.strip() for m in os.getenv(
    "MODEL_FALLBACKS", "gpt-4o,gpt-4o-mini").split(",") if m.strip()]
# Narrow, high-volume text jobs (picking a sheet tab) — cheap is fine.
FAST_MODEL = os.getenv("FAST_MODEL", "gpt-4o-mini").strip()
# Reading receipts. MUST be a model that actually sees images — some gateway
# aliases accept an image and silently return nulls, which loses every receipt.
VISION_MODEL = os.getenv("VISION_MODEL", "gpt-4o").strip()

# Semantic memory. Embeddings let "what did I decide about caching" find the
# right message even when it shares no keyword with what was written. If the
# endpoint doesn't serve embeddings (some gateways don't), recall degrades to
# keyword search instead of breaking — see brain/memory.py.
EMBED_MODEL = os.getenv("EMBED_MODEL", "text-embedding-3-small").strip()
EMBED_ENABLED = os.getenv("EMBED_ENABLED", "1").strip().lower() not in ("0", "false", "no")
# Embeddings may live on a DIFFERENT provider than chat. A gateway that serves
# chat perfectly can still refuse embeddings — llms.codershive.in returns
# 400 "No credentials for embedding". Set EMBED_API_KEY to an OpenAI key (and
# leave EMBED_BASE_URL empty for api.openai.com) and chat keeps using the
# gateway while embeddings go direct. Unset = use the same client as chat.
EMBED_BASE_URL = os.getenv("EMBED_BASE_URL", "").strip()
EMBED_API_KEY = os.getenv("EMBED_API_KEY", "").strip()
# How many past items the semantic search may consider. Cosine over a few
# thousand short vectors in Python is milliseconds; this is just a sanity cap.
MEMORY_SCAN_LIMIT = int(os.getenv("MEMORY_SCAN_LIMIT", "4000"))
# How many undo points to keep per user. Each is a snapshot of that user's
# plan + reminders, so this is kilobytes, not megabytes.
UNDO_HISTORY = int(os.getenv("UNDO_HISTORY", "20"))
# Evening "is it done?" pass. Local hour, 24h. This is a question, not another
# list — see scheduler/jobs.evening_checkin.
CHECKIN_HOUR = int(os.getenv("CHECKIN_HOUR", "21"))

# --- Where the user actually lives -----------------------------------------
# Drives how times, dates, money and number words are read. Wrong defaults here
# are silent and expensive: "2 lakh" logged as 2, or 03-04 read as 4 March.
COUNTRY = os.getenv("COUNTRY", "India").strip()
CURRENCY = os.getenv("CURRENCY", "INR").strip()
CURRENCY_SYMBOL = os.getenv("CURRENCY_SYMBOL", "₹").strip()
# Day-first, like the rest of the world outside the US.
DATE_ORDER = os.getenv("DATE_ORDER", "DD-MM-YYYY").strip()

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

# --- Obsidian vault ---------------------------------------------------------
# Obsidian has no API — it's a folder of .md files — so notes are written into
# somewhere the user's vault syncs from. Normally each user links their own
# (GitHub repo or folder) with /vault; VAULT_DIR is only the fallback for a
# self-hosted, one-person install where the bot sits next to the vault.
VAULT_DIR = os.getenv("VAULT_DIR", "").strip()
# With several users on one server, give each their own subfolder under it,
# otherwise everyone writes into the same notes — the multi-tenant rule.
VAULT_PER_USER_SUBDIR = os.getenv(
    "VAULT_PER_USER_SUBDIR", "1").strip().lower() not in ("0", "false", "no")
# Folder names inside the vault. Changing these changes where new notes land;
# notes already written stay where they are.
VAULT_DAILY_FOLDER = os.getenv("VAULT_DAILY_FOLDER", "Daily").strip()
VAULT_INBOX_FOLDER = os.getenv("VAULT_INBOX_FOLDER", "Inbox").strip()
VAULT_DEFAULT_FOLDER = os.getenv("VAULT_DEFAULT_FOLDER", "Notes").strip()

# --- Scheduler (Phase 4) ---
# Timezone for all scheduled jobs. India by default.
TIMEZONE = os.getenv("TIMEZONE", "Asia/Kolkata").strip()
# Hour (0-23, local time) the daily job runs to check statements + due dates.
DAILY_JOB_HOUR = int(os.getenv("DAILY_JOB_HOUR", "9"))
# How many days before a bill's due date to send a reminder.
DUE_REMINDER_DAYS = int(os.getenv("DUE_REMINDER_DAYS", "3"))

# --- Build marker ---
# Bumped whenever the bot's behaviour changes, so /version tells you instantly
# whether the deployment actually picked up the latest code.
BUILD = os.getenv("BUILD", "2026-07-31.1 jarvis").strip()
