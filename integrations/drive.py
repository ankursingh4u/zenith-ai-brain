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


def _make_public(svc, file_id: str) -> None:
    """Anyone with the link can view — so the link pasted into a sheet actually opens."""
    svc.permissions().create(
        fileId=file_id, body={"role": "reader", "type": "anyone"},
        supportsAllDrives=True,
    ).execute()


def upload_file(telegram_id: int, filename: str, content: bytes, mime_type: str,
                public: bool = True) -> str:
    """Upload into the user's registered folder. Returns a shareable view link.

    With `public` (the default) the file is given 'anyone with the link can view',
    so the link is usable from a spreadsheet cell or shared with anyone.
    """
    _, folder_id = db.get_user_resources(telegram_id)
    if not folder_id:
        raise NoFolder("No Drive folder registered.")
    media = MediaIoBaseUpload(io.BytesIO(content), mimetype=mime_type, resumable=False)
    svc = gservice.drive(telegram_id)
    created = svc.files().create(
        body={"name": filename, "parents": [folder_id]},
        media_body=media, fields="id, webViewLink", supportsAllDrives=True,
    ).execute()
    file_id = created["id"]
    if public:
        try:
            _make_public(svc, file_id)
        except Exception:  # noqa: BLE001 — folder policy may forbid link sharing
            pass
    return created.get("webViewLink") or f"https://drive.google.com/file/d/{file_id}/view"


# -------------------------------------------------------------------------
#  Fallback: upload into a linked (OAuth) Google account's own Drive.
#  A service account has no My Drive storage, so without a shared folder the
#  only place a file can go is the user's own account.
# -------------------------------------------------------------------------
_UPLOAD_FOLDER = "Brain Uploads"


def _own_folder(svc) -> str:
    """Find or create the 'Brain Uploads' folder in the user's own Drive."""
    q = (f"name = '{_UPLOAD_FOLDER}' and mimeType = 'application/vnd.google-apps.folder' "
         f"and trashed = false")
    found = svc.files().list(q=q, pageSize=1, fields="files(id)").execute().get("files", [])
    if found:
        return found[0]["id"]
    created = svc.files().create(
        body={"name": _UPLOAD_FOLDER, "mimeType": "application/vnd.google-apps.folder"},
        fields="id",
    ).execute()
    return created["id"]


def upload_to_account(telegram_id: int, email: str, filename: str, content: bytes,
                      mime_type: str, public: bool = True) -> str:
    """Upload into a linked Google account's own Drive ('Brain Uploads' folder)."""
    from integrations import client as goauth

    svc = goauth.drive(telegram_id, email)
    media = MediaIoBaseUpload(io.BytesIO(content), mimetype=mime_type, resumable=False)
    created = svc.files().create(
        body={"name": filename, "parents": [_own_folder(svc)]},
        media_body=media, fields="id, webViewLink",
    ).execute()
    file_id = created["id"]
    if public:
        try:
            _make_public(svc, file_id)
        except Exception:  # noqa: BLE001
            pass
    return created.get("webViewLink") or f"https://drive.google.com/file/d/{file_id}/view"


def upload_beside_sheet(telegram_id: int, filename: str, content: bytes,
                        mime_type: str, public: bool = True) -> str:
    """Upload into the folder that already contains the user's connected sheet.

    Needs no extra setup: if the user shared the sheet's folder (or their whole
    Drive) with us, its parent is writable and the file lands right next to the
    sheet it belongs to.
    """
    sheet_id = db.default_sheet_id(telegram_id)
    if not sheet_id:
        raise NoFolder("No sheet connected.")
    svc = gservice.drive(telegram_id)
    parents = svc.files().get(
        fileId=sheet_id, fields="parents", supportsAllDrives=True
    ).execute().get("parents") or []
    if not parents:
        raise NoFolder("Can't see the sheet's folder.")
    media = MediaIoBaseUpload(io.BytesIO(content), mimetype=mime_type, resumable=False)
    created = svc.files().create(
        body={"name": filename, "parents": [parents[0]]},
        media_body=media, fields="id, webViewLink", supportsAllDrives=True,
    ).execute()
    file_id = created["id"]
    if public:
        try:
            _make_public(svc, file_id)
        except Exception:  # noqa: BLE001
            pass
    return created.get("webViewLink") or f"https://drive.google.com/file/d/{file_id}/view"


def save_anywhere(telegram_id: int, filename: str, content: bytes,
                  mime_type: str) -> tuple[str | None, str]:
    """Best-effort public upload, trying every place we might be able to write:
    the registered folder, the sheet's own folder, then a linked account's Drive.
    Returns (link_or_None, detail_message)."""
    errors = []
    _, folder_id = db.get_user_resources(telegram_id)
    if folder_id:
        try:
            return upload_file(telegram_id, filename, content, mime_type), "your Drive folder"
        except Exception as e:  # noqa: BLE001
            errors.append(f"shared folder: {e}")
    try:
        return (upload_beside_sheet(telegram_id, filename, content, mime_type),
                "the folder holding your sheet")
    except Exception as e:  # noqa: BLE001
        errors.append(f"sheet folder: {e}")
    for acct in db.list_google_accounts(telegram_id):
        try:
            link = upload_to_account(telegram_id, acct.email, filename, content, mime_type)
            return link, f"{acct.email} → {_UPLOAD_FOLDER}"
        except Exception as e:  # noqa: BLE001
            errors.append(f"{acct.email}: {e}")
    if errors:
        return None, "; ".join(errors)
    return None, ("no Drive connected — share a Drive folder with me (send its link) "
                  "or link a Google account with /connect")
