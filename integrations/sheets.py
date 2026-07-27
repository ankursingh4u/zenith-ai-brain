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
    count = db.add_sheet(telegram_id, sheet_id, detail)
    if count == 1:
        return True, f"✅ Connected your sheet '{detail}'. New entries will be saved here."
    return True, (f"✅ Connected '{detail}'. You now have {count} sheets connected. "
                  f"Entries go to your default sheet — use /sheets to see them or switch.")


def _sheet_id(telegram_id: int, sheet_id: str | None = None) -> str:
    sheet_id = sheet_id or db.default_sheet_id(telegram_id)
    if not sheet_id:
        raise NoSheet("No sheet registered.")
    return sheet_id


def _quote(tab: str) -> str:
    """A1 range needs the tab name quoted when it has spaces/punctuation."""
    return "'" + tab.replace("'", "''") + "'"


def _append(telegram_id: int, row: list, tab: str | None = None,
            sheet_id: str | None = None) -> None:
    sid = _sheet_id(telegram_id, sheet_id)
    rng = f"{_quote(tab)}!A1" if tab else "A1"
    gservice.sheets(telegram_id).spreadsheets().values().append(
        spreadsheetId=sid, range=rng,
        valueInputOption="USER_ENTERED", insertDataOption="INSERT_ROWS",
        body={"values": [row]},
    ).execute()


# -------------------------------------------------------------------------
#  Tabs & headers — a single sheet usually has many tabs (EXPENSES, SWIPE...)
#  with their own column layout. We read that layout instead of assuming one.
# -------------------------------------------------------------------------
def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


# Words the user/AI might use for a column that is named differently in the sheet.
_SYNONYMS: dict[str, tuple[str, ...]] = {
    "date": ("date", "day", "txndate", "entrydate"),
    "amount": ("amount", "amt", "value", "rs", "rupees", "total"),
    "transferto": ("transferto", "to", "paidto", "payee", "receiver", "party", "vendor",
                   "merchant", "name"),
    "transferfrom": ("transferfrom", "from", "paidfrom", "source", "sender", "fromacc",
                     "fromaccount"),
    "reason": ("reason", "note", "notes", "remark", "remarks", "description", "purpose",
               "particulars", "details", "category"),
    "paymentmode": ("paymentmode", "mode", "method", "paymentmethod", "via", "channel"),
    "account": ("account", "bank", "bankname", "acc", "accountname"),
    "images": ("images", "image", "screenshot", "screenshots", "photo", "attachment",
               "attachments", "link", "proof", "receipt", "bill"),
    "empname": ("empname", "employee", "employeename", "staff", "addedby", "by", "user"),
}


def _synonym_group(token: str) -> tuple[str, ...]:
    n = _norm(token)
    for group in _SYNONYMS.values():
        if n in group:
            return group
    return (n,)


def list_tabs(telegram_id: int, sheet_id: str | None = None) -> list[str]:
    """Every tab (worksheet) name in the spreadsheet, in sheet order."""
    sid = _sheet_id(telegram_id, sheet_id)
    meta = gservice.sheets(telegram_id).spreadsheets().get(
        spreadsheetId=sid, fields="sheets.properties.title"
    ).execute()
    return [s["properties"]["title"] for s in meta.get("sheets", [])]


def resolve_tab(telegram_id: int, hint: str | None, sheet_id: str | None = None) -> str | None:
    """Map a loose name ('Expense', 'expenses tab') to a real tab title."""
    if not hint:
        return None
    tabs = list_tabs(telegram_id, sheet_id)
    h = _norm(re.sub(r"\btabs?\b", "", hint, flags=re.I))
    if not h:
        return None
    for t in tabs:                                  # exact (normalised)
        if _norm(t) == h:
            return t
    for t in tabs:                                  # singular/plural & partial
        n = _norm(t)
        if n.startswith(h) or h.startswith(n) or h in n or n in h:
            return t
    return None


def find_column(headers: list[str], *names: str) -> str | None:
    """The header matching any of `names` (ignoring case/spacing), or None."""
    wanted = {_norm(n) for n in names}
    for h in headers:
        if _norm(h) in wanted:
            return h
    return None


def tab_from_text(telegram_id: int, text: str, sheet_id: str | None = None) -> str | None:
    """Spot a tab name inside a free-text instruction, e.g. '... (Expense) tab'."""
    if not text:
        return None
    tabs = list_tabs(telegram_id, sheet_id)
    body = _norm(text)
    # Longest tab names first, so 'BANK TRANSFER' wins over a 'BANK' tab.
    for t in sorted(tabs, key=lambda x: -len(_norm(x))):
        n = _norm(t)
        if len(n) < 3:
            continue
        stem = n[:-1] if n.endswith("s") else n          # EXPENSES -> EXPENSE
        if n in body or (len(stem) >= 4 and stem in body):
            return t
    return None


def tab_headers(telegram_id: int, tab: str | None = None,
                sheet_id: str | None = None) -> list[str]:
    """The header row (row 1) of a tab. Empty list if the tab has no headers."""
    sid = _sheet_id(telegram_id, sheet_id)
    rng = f"{_quote(tab)}!A1:Z1" if tab else "A1:Z1"
    try:
        resp = gservice.sheets(telegram_id).spreadsheets().values().get(
            spreadsheetId=sid, range=rng
        ).execute()
    except Exception:  # noqa: BLE001 — tab missing / empty
        return []
    rows = resp.get("values", [])
    return [str(c).strip() for c in rows[0]] if rows else []


def describe_structure(telegram_id: int, sheet_id: str | None = None) -> str:
    """Human/AI readable map of the sheet: every tab and its columns."""
    tabs = list_tabs(telegram_id, sheet_id)
    if not tabs:
        return "This sheet has no tabs."
    lines = []
    for t in tabs:
        cols = tab_headers(telegram_id, t, sheet_id)
        lines.append(f"• {t} — columns: " + (", ".join(cols) if cols else "(no header row)"))
    return "\n".join(lines)


def append_mapped(telegram_id: int, fields: dict, tab: str | None = None,
                  sheet_id: str | None = None) -> tuple[str, list[str]]:
    """Append a row, placing each value under the column that matches its name.

    `fields` is {column-ish name: value}. Matching is case/space-insensitive with
    synonyms, so 'to'/'paid to' both land in a 'TRANSFER TO' column. Returns
    (tab_used, unmatched_field_names).
    """
    real_tab = resolve_tab(telegram_id, tab, sheet_id) or tab
    headers = tab_headers(telegram_id, real_tab, sheet_id)
    if not headers:
        # An empty tab (no header row yet) — borrow the layout from the first tab
        # that has one, so the columns still line up with the rest of the sheet.
        for t in list_tabs(telegram_id, sheet_id):
            headers = tab_headers(telegram_id, t, sheet_id)
            if headers:
                break
    if not headers:                       # a sheet with no headers anywhere
        _append(telegram_id, [str(v) for v in fields.values()], real_tab, sheet_id)
        return (real_tab or "first tab"), []

    # Build candidate keys for each supplied field, then fill each header slot once.
    remaining = {k: v for k, v in fields.items() if v not in (None, "")}
    row = [""] * len(headers)
    for i, head in enumerate(headers):
        hnorm = _norm(head)
        hgroup = _synonym_group(head)
        hit = None
        for key in remaining:                                   # exact name match
            if _norm(key) == hnorm:
                hit = key
                break
        if hit is None:                                         # synonym match
            for key in remaining:
                if _norm(key) in hgroup or _synonym_group(key) == hgroup:
                    hit = key
                    break
        if hit is None:                                         # loose containment
            for key in remaining:
                k = _norm(key)
                if k and hnorm and (k in hnorm or hnorm in k):
                    hit = key
                    break
        if hit is not None:
            row[i] = str(remaining.pop(hit))
    _append(telegram_id, row, real_tab, sheet_id)
    return (real_tab or "first tab"), list(remaining)


def append_transaction(
    telegram_id: int, amount: float, kind: str, category: str | None, note: str | None,
    tab: str | None = None, extra: dict | None = None,
) -> str:
    """Log a transaction. If the target tab has its own header row we fill THOSE
    columns; otherwise we fall back to our simple 5-column layout."""
    real_tab = resolve_tab(telegram_id, tab) or tab
    headers = tab_headers(telegram_id, real_tab)
    if headers:
        fields = {
            "date": datetime.now().strftime("%d/%m/%Y"),
            "amount": f"{amount:.2f}",
            "reason": note or category or "",
            "type": "IN" if kind == "in" else "OUT",
        }
        fields.update({k: v for k, v in (extra or {}).items() if v})
        used, _ = append_mapped(telegram_id, fields, real_tab)
        return used
    _append(telegram_id, [
        datetime.now().strftime("%Y-%m-%d %H:%M"),
        "IN" if kind == "in" else "OUT",
        f"{amount:.2f}", category or "", note or "",
    ], real_tab)
    return real_tab or "first tab"


def append_row(telegram_id: int, values: list) -> None:
    _append(telegram_id, list(values))


def read_rows(telegram_id: int, limit: int = 100, tab: str | None = None) -> list[list]:
    sid = _sheet_id(telegram_id)
    real_tab = resolve_tab(telegram_id, tab) or tab
    rng = f"{_quote(real_tab)}!A1:Z1000" if real_tab else "A1:Z1000"
    resp = gservice.sheets(telegram_id).spreadsheets().values().get(
        spreadsheetId=sid, range=rng
    ).execute()
    values = resp.get("values", [])
    if len(values) <= 1:
        return values
    return [values[0]] + values[1:][-limit:]
