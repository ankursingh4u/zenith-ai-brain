"""Build authenticated Google API clients for a specific user + account.

Everything takes telegram_id AND the account email, and loads THAT account's
encrypted token. There's no way to build a client without a user id, so cross-user
access can't happen; and the email selects which of the user's linked accounts.
"""
from __future__ import annotations

import json

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

import config
import crypto
import db


class NotConnected(Exception):
    """Raised when the user has no linked Google account."""


def _load_credentials(telegram_id: int, email: str) -> Credentials:
    token_enc = db.get_account_token_enc(telegram_id, email)
    if not token_enc:
        raise NotConnected(f"No linked Google account '{email}'. Send /connect first.")

    creds = Credentials.from_authorized_user_info(
        json.loads(crypto.decrypt(token_enc)), scopes=config.GOOGLE_SCOPES
    )
    # Refresh silently if expired, and persist the new token for that account.
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
        db.add_google_account(telegram_id, email, crypto.encrypt(creds.to_json()))
    return creds


def gmail(telegram_id: int, email: str):
    return build("gmail", "v1", credentials=_load_credentials(telegram_id, email))


def sheets(telegram_id: int, email: str):
    return build("sheets", "v4", credentials=_load_credentials(telegram_id, email))


def drive(telegram_id: int, email: str):
    return build("drive", "v3", credentials=_load_credentials(telegram_id, email))


def calendar(telegram_id: int, email: str):
    return build("calendar", "v3", credentials=_load_credentials(telegram_id, email))
