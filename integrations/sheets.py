"""Write/read the user's OWN Google Sheet via the service account.

The user shares their sheet with the bot's service-account email and registers its
link. We only ever touch the sheet id mapped to that telegram_id — full isolation.
"""
from __future__ import annotations

import re
from datetime import datetime

import db
from integrations import gservice

_SHEET_ID_RE = re.compile(r"/spreadsheets/d/([a-zA-Z0-9-_]+)")


class NoSheet(Exception):
    """User hasn't registered a shared sheet yet."""


def extract_sheet_id(text: str) -> str | None:
    """Pull the spreadsheet id from a full URL, or accept a bare id."""
    m = _SHEET_ID_RE.search(text or "")
    if m:
        return m.group(1)
    token = (text or "").strip()
    if re.fullmatch(r"[a-zA-Z0-9-_]{20,}", token):   # looks like a bare id
        return token
    return None


def verify_access(telegram_id: int, sheet_id: str) -> tuple[bool, str]:
    """Check the service account can actually read the sheet. Returns (ok, detail)."""
    try:
        meta = gservice.sheets(telegram_id).spreadsheets().get(
            spreadsheetId=sheet_id, fields="properties.title"
        ).execute()
        return True, meta["properties"]["title"]
    except Exception as e:  # noqa: BLE001
        return False, str(e)


def register(telegram_id: int, url_or_id: str) -> tuple[bool, str]:
    """Register a user's sheet after confirming access. Returns (ok, message)."""
    sheet_id = extract_sheet_id(url_or_id)
    if not sheet_id:
        return False, "That doesn't look like a Google Sheet link. Paste the full sheet URL."
    ok, detail = verify_access(telegram_id, sheet_id)
    if not ok:
        if "SERVICE_DISABLED" in detail or "has not been used in project" in detail:
            return False, ("⚙️ The Google Sheets API isn't enabled on the bot's project yet. "
                           "The owner needs to enable it in Google Cloud Console, wait a minute, "
                           "then resend the link.")
        email = gservice.service_account_email(telegram_id) or "the bot's service account"
        return False, (f"I can't open that sheet yet. In the sheet click Share, add "
                       f"{email} as Editor, then send the link again.")
    db.set_user_resources(telegram_id, sheet_id=sheet_id)
    return True, f"✅ Connected your sheet '{detail}'. I'll record entries here now."


def _sheet_id(telegram_id: int) -> str:
    sheet_id, _ = db.get_user_resources(telegram_id)
    if not sheet_id:
        raise NoSheet("No sheet registered.")
    return sheet_id


def _append(telegram_id: int, row: list, tab: str | None = None) -> None:
    sheet_id = _sheet_id(telegram_id)
    rng = f"{tab}!A1" if tab else "A1"
    gservice.sheets(telegram_id).spreadsheets().values().append(
        spreadsheetId=sheet_id, range=rng,
        valueInputOption="USER_ENTERED", insertDataOption="INSERT_ROWS",
        body={"values": [row]},
    ).execute()


def append_transaction(
    telegram_id: int, amount: float, kind: str, category: str | None, note: str | None
) -> None:
    _append(telegram_id, [
        datetime.now().strftime("%Y-%m-%d %H:%M"),
        "IN" if kind == "in" else "OUT",
        f"{amount:.2f}", category or "", note or "",
    ])


def append_row(telegram_id: int, values: list) -> None:
    _append(telegram_id, list(values))


def read_rows(telegram_id: int, limit: int = 100) -> list[list]:
    sheet_id = _sheet_id(telegram_id)
    resp = gservice.sheets(telegram_id).spreadsheets().values().get(
        spreadsheetId=sheet_id, range="A1:Z1000"
    ).execute()
    values = resp.get("values", [])
    if len(values) <= 1:
        return values
    return [values[0]] + values[1:][-limit:]
