"""Upload receipts to the user's OWN Drive folder via the service account.

The user shares a Drive folder with the bot's service-account email and registers
its link. Files are uploaded into that user-owned folder.
"""
from __future__ import annotations

import io
import re

from googleapiclient.http import MediaIoBaseUpload

import db
from integrations import gservice

_FOLDER_ID_RE = re.compile(r"/folders/([a-zA-Z0-9-_]+)")


class NoFolder(Exception):
    """User hasn't registered a shared Drive folder yet."""


def extract_folder_id(text: str) -> str | None:
    m = _FOLDER_ID_RE.search(text or "")
    if m:
        return m.group(1)
    token = (text or "").strip()
    if re.fullmatch(r"[a-zA-Z0-9-_]{20,}", token):
        return token
    return None


def verify_access(telegram_id: int, folder_id: str) -> tuple[bool, str]:
    try:
        meta = gservice.drive(telegram_id).files().get(
            fileId=folder_id, fields="name, mimeType"
        ).execute()
        if meta.get("mimeType") != "application/vnd.google-apps.folder":
            return False, "That link is not a folder."
        return True, meta["name"]
    except Exception as e:  # noqa: BLE001
        return False, str(e)


def register(telegram_id: int, url_or_id: str) -> tuple[bool, str]:
    folder_id = extract_folder_id(url_or_id)
    if not folder_id:
        return False, "That doesn't look like a Drive folder link."
    ok, detail = verify_access(telegram_id, folder_id)
    if not ok:
        if "SERVICE_DISABLED" in detail or "has not been used in project" in detail:
            return False, ("⚙️ The Google Drive API isn't enabled on the bot's project yet. "
                           "The owner needs to enable it in Google Cloud Console, wait a minute, "
                           "then resend the link.")
        email = gservice.service_account_email(telegram_id) or "the bot's service account"
        return False, (f"I can't open that folder yet. Share it with {email} as "
                       f"Editor, then send the link again.")
    db.set_user_resources(telegram_id, folder_id=folder_id)
    return True, f"✅ Connected your Drive folder '{detail}'. Bills will be saved here."


def upload_file(telegram_id: int, filename: str, content: bytes, mime_type: str) -> str:
    """Upload into the user's registered folder. Returns a view link."""
    _, folder_id = db.get_user_resources(telegram_id)
    if not folder_id:
        raise NoFolder("No Drive folder registered.")
    media = MediaIoBaseUpload(io.BytesIO(content), mimetype=mime_type, resumable=False)
    created = gservice.drive(telegram_id).files().create(
        body={"name": filename, "parents": [folder_id]},
        media_body=media, fields="id, webViewLink",
    ).execute()
    return created.get("webViewLink", "")
