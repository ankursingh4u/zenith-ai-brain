"""Per-user Google OAuth: build the login link and handle the callback.

Flow:
  1. User sends /connect in Telegram.
  2. We create a random `state`, tie it to their telegram_id, and DM them a
     Google login URL.
  3. They log into THEIR Google and approve. Google redirects to our FastAPI
     callback with `code` + `state`.
  4. We verify `state`, exchange the code for a token, encrypt it, and store it
     against that telegram_id.

Because each user authorises their own Google account, one user's Gmail/Sheets/
Drive is never reachable by another.
"""
from __future__ import annotations

import os

# Google sometimes returns a superset of scopes (adds 'openid'); relax so the
# token exchange doesn't raise on a harmless scope-order/superset change.
os.environ.setdefault("OAUTHLIB_RELAX_TOKEN_SCOPE", "1")

from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build

import json

import config
import crypto
import db


def _client_info(telegram_id: int | None) -> dict | None:
    """Parsed OAuth client JSON — EACH user's OWN console, nothing shared."""
    if telegram_id is None:
        return None
    raw = db.get_custom_oauth_enc(telegram_id)
    if raw:
        return json.loads(crypto.decrypt(raw))
    return None


def is_configured(telegram_id: int | None = None) -> bool:
    return _client_info(telegram_id) is not None


def _client_config(telegram_id: int | None = None) -> dict:
    info = _client_info(telegram_id)
    web = info.get("web") or info.get("installed") or info
    return {
        "web": {
            "client_id": web["client_id"],
            "client_secret": web["client_secret"],
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": [config.OAUTH_REDIRECT_URI],
        }
    }


def save_client_json(text: str, telegram_id: int) -> tuple[bool, str]:
    """Validate & store a pasted OAuth client JSON as THIS user's own console."""
    try:
        info = json.loads(text)
    except Exception:  # noqa: BLE001
        return False, "not valid JSON"
    web = info.get("web") or info.get("installed")
    if not web or not web.get("client_id") or not web.get("client_secret"):
        return False, "not an OAuth client JSON (need client_id + client_secret)"
    db.set_custom_oauth(telegram_id, crypto.encrypt(json.dumps(info)))
    return True, web["client_id"]


def build_login_url(telegram_id: int) -> str:
    """Create a state, tie it to the user, and return the Google consent URL."""
    if not is_configured(telegram_id):
        raise RuntimeError("Google login isn't configured yet.")

    state = db.create_oauth_state(telegram_id)
    flow = Flow.from_client_config(
        _client_config(telegram_id), scopes=config.GOOGLE_SCOPES,
        redirect_uri=config.OAUTH_REDIRECT_URI,
    )
    auth_url, _ = flow.authorization_url(
        access_type="offline",       # get a refresh token
        prompt="consent",            # force refresh token on re-connect
        include_granted_scopes="true",
        state=state,
    )
    return auth_url


# --- FastAPI callback server ---------------------------------------------
app = FastAPI(title="Brain OAuth")


def _page(title: str, body: str) -> HTMLResponse:
    return HTMLResponse(
        f"<html><body style='font-family:sans-serif;text-align:center;padding:40px'>"
        f"<h2>{title}</h2><p>{body}</p>"
        f"<p>You can close this tab and return to Telegram.</p></body></html>"
    )


@app.get("/oauth/callback")
def oauth_callback(state: str = "", code: str = "", error: str = ""):
    if error:
        return _page("❌ Connection cancelled", f"Google said: {error}")

    telegram_id = db.consume_oauth_state(state)
    if telegram_id is None:
        return _page("❌ Invalid or expired link", "Please run /connect again in Telegram.")

    try:
        flow = Flow.from_client_config(
            _client_config(telegram_id), scopes=config.GOOGLE_SCOPES,
            redirect_uri=config.OAUTH_REDIRECT_URI, state=state,
        )
        flow.fetch_token(code=code)
        creds = flow.credentials

        # Find out which Google account this is.
        info = build("oauth2", "v2", credentials=creds).userinfo().get().execute()
        email = info.get("email", "unknown")

        # Add (not overwrite) — a user can link several Google accounts.
        db.add_google_account(telegram_id, email, crypto.encrypt(creds.to_json()))
    except Exception as e:  # noqa: BLE001
        return _page("❌ Something went wrong", f"{e}")

    return _page("✅ Google account linked!",
                 f"Linked: <b>{email}</b><br>Return to Telegram. "
                 f"To add another account, send /connect again and pick a different Google login.")


@app.get("/")
def health():
    return {"status": "ok"}
