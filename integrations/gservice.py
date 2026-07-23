"""Service-account Google clients (share-a-sheet model).

By default the bot uses ONE shared service account. A user may optionally plug in
their OWN service-account key (stored encrypted) for full control of their own
Google project — in that case their operations use their own credentials + email.
Isolation is enforced in our code: each telegram_id maps to that user's own resources.
"""
from __future__ import annotations

import json
import os

from google.oauth2 import service_account
from googleapiclient.discovery import build

import config
import crypto
import db

_SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

_shared_creds = None
_shared_email: str | None = None


class NotSetUp(Exception):
    """Raised when no service-account key (shared or custom) is available."""


def _sa_info_from_env() -> dict | None:
    """Shared service-account JSON straight from an env var (for hosting — no file)."""
    raw = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON", "").strip()
    if raw:
        try:
            return json.loads(raw)
        except Exception:  # noqa: BLE001
            return None
    return None


def is_configured() -> bool:
    """True if a shared service account is available (env var or key file)."""
    if _sa_info_from_env() is not None:
        return True
    return bool(config.GOOGLE_SERVICE_ACCOUNT_FILE) and os.path.exists(
        config.GOOGLE_SERVICE_ACCOUNT_FILE
    )


def available_for(telegram_id: int | None) -> bool:
    """True if this user can use Google (their own custom key, or the shared one)."""
    if telegram_id is not None and db.get_custom_sa_enc(telegram_id):
        return True
    return is_configured()


# --- shared credentials ---------------------------------------------------
def _shared_credentials():
    global _shared_creds
    if _shared_creds is None:
        env_info = _sa_info_from_env()
        if env_info is not None:
            _shared_creds = service_account.Credentials.from_service_account_info(
                env_info, scopes=_SCOPES)
        elif config.GOOGLE_SERVICE_ACCOUNT_FILE and os.path.exists(config.GOOGLE_SERVICE_ACCOUNT_FILE):
            _shared_creds = service_account.Credentials.from_service_account_file(
                config.GOOGLE_SERVICE_ACCOUNT_FILE, scopes=_SCOPES)
        else:
            raise NotSetUp("Shared service account not configured.")
    return _shared_creds


def _shared_email_addr() -> str | None:
    global _shared_email
    if _shared_email is None:
        env_info = _sa_info_from_env()
        if env_info is not None:
            _shared_email = env_info.get("client_email")
        elif config.GOOGLE_SERVICE_ACCOUNT_FILE and os.path.exists(config.GOOGLE_SERVICE_ACCOUNT_FILE):
            with open(config.GOOGLE_SERVICE_ACCOUNT_FILE, encoding="utf-8") as f:
                _shared_email = json.load(f).get("client_email")
    return _shared_email


# --- per-user credentials -------------------------------------------------
def _custom_info(telegram_id: int) -> dict | None:
    enc = db.get_custom_sa_enc(telegram_id)
    if not enc:
        return None
    return json.loads(crypto.decrypt(enc))


def _credentials(telegram_id: int | None):
    if telegram_id is not None:
        info = _custom_info(telegram_id)
        if info:
            return service_account.Credentials.from_service_account_info(info, scopes=_SCOPES)
    return _shared_credentials()


def service_account_email(telegram_id: int | None = None) -> str | None:
    """The email a user should share their sheet with (their custom SA, else shared)."""
    if telegram_id is not None:
        info = _custom_info(telegram_id)
        if info:
            return info.get("client_email")
    return _shared_email_addr()


def sheets(telegram_id: int | None = None):
    return build("sheets", "v4", credentials=_credentials(telegram_id))


def drive(telegram_id: int | None = None):
    return build("drive", "v3", credentials=_credentials(telegram_id))


def reload_shared() -> None:
    """Forget cached shared credentials so the key file is re-read."""
    global _shared_creds, _shared_email
    _shared_creds = None
    _shared_email = None


def set_default_key(text: str) -> tuple[bool, str]:
    """Replace the SHARED/default service-account key with a pasted one (owner only).
    Returns (ok, email_or_error)."""
    ok, info = validate_key_json(text)
    if not ok:
        return False, info
    with open(config.GOOGLE_SERVICE_ACCOUNT_FILE, "w", encoding="utf-8") as f:
        f.write(text)
    reload_shared()
    return True, info


def validate_key_json(text: str) -> tuple[bool, str]:
    """Check a pasted string is a usable service-account key. Returns (ok, email_or_error)."""
    try:
        info = json.loads(text)
    except Exception:  # noqa: BLE001
        return False, "not valid JSON"
    if info.get("type") != "service_account" or not info.get("private_key") \
            or not info.get("client_email"):
        return False, "not a service-account key"
    try:
        service_account.Credentials.from_service_account_info(info, scopes=_SCOPES)
    except Exception as e:  # noqa: BLE001
        return False, f"key rejected: {e}"
    return True, info["client_email"]
