"""Tools the AI brain can call.

Each tool takes `telegram_id` as its FIRST argument (injected by the agent, never
by the model) so every action is scoped to the calling user. The model only ever
supplies the business arguments. This is what keeps one user's actions from ever
touching another user's data.

Google model = share-a-sheet: the user shares their own Google Sheet / Drive folder
with the bot's service-account email, then registers the link. Everything else
(reminders, vault, transactions) works with no Google at all.
"""
from __future__ import annotations

import contextvars
from datetime import datetime
from zoneinfo import ZoneInfo

from sqlalchemy import select

import config
import crypto
import db
from brain import money
from integrations import calendar as gcal
from integrations import client as goauth
from integrations import docs as gdocs
from integrations import drive, gmail, gservice, mailbox, pdf, sheets

_OAUTH_HINT = ("To use email / calendar / docs / drive, connect a Google account first — "
               "send /connect (you can link several).")


def _resolve_account(telegram_id: int, account: str | None) -> tuple[str | None, str | None]:
    """Pick which linked Google account to use. Returns (email, ask_message).

    - No accounts → (None, hint to connect).
    - `account` given → match by (partial) email.
    - Exactly one account → use it.
    - Multiple & none chosen → (None, a 'which account?' question) so the AI asks.
    """
    accts = db.list_google_accounts(telegram_id)
    if not accts:
        return None, _OAUTH_HINT
    if account:
        match = [a for a in accts if account.lower() in a.email.lower()]
        if match:
            return match[0].email, None
        return None, (f"No linked account matches '{account}'. Linked: "
                      + ", ".join(a.email for a in accts))
    # Use the user's chosen default account, if still valid.
    default = db.get_default_account(telegram_id)
    if default and any(a.email == default for a in accts):
        return default, None
    if len(accts) == 1:
        return accts[0].email, None
    return None, ("You have multiple Google accounts linked:\n"
                  + "\n".join(f"• {a.email}" for a in accts)
                  + "\nWhich one should I use? (Tip: set a default in /connect so I stop asking.)")

_TZ = ZoneInfo(config.TIMEZONE)
_UTC = ZoneInfo("UTC")

# The exact text the user typed this turn — for audit + amount cross-check.
# A ContextVar so concurrent users never mix (each turn runs in its own context).
_current_message: contextvars.ContextVar[str] = contextvars.ContextVar("_msg", default="")


def set_current_message(text: str) -> None:
    _current_message.set(text or "")


# The last image each user sent, so a follow-up ("put it on my drive") still works.
# Small and short-lived: {telegram_id: (content, mime, filename)}.
_last_image: dict[int, tuple[bytes, str, str]] = {}


def remember_image(telegram_id: int, content: bytes, mime: str, filename: str) -> None:
    _last_image[telegram_id] = (content, mime, filename)
    if len(_last_image) > 50:                      # keep the cache bounded
        for k in list(_last_image)[:-50]:
            _last_image.pop(k, None)


def _has_sheet(telegram_id: int) -> bool:
    return db.count_sheets(telegram_id) > 0


# =========================================================================
#  Money / accountant
# =========================================================================
def log_transaction(
    telegram_id: int, amount: float, kind: str = "out",
    category: str | None = None, note: str | None = None,
) -> str:
    kind = "in" if str(kind).lower() in ("in", "credit", "income", "received") else "out"
    amount = abs(float(amount))
    raw = _current_message.get()
    with db.session() as s:
        s.add(db.Transaction(telegram_id=telegram_id, amount=amount, kind=kind,
                             category=category, note=note, raw_text=raw))
        s.commit()
    arrow = "received" if kind == "in" else "paid"
    label = f" for {category}" if category else ""

    # Safety nets: independent amount re-check + large-amount nudge.
    guards = []
    warn = money.mismatch_warning(amount, raw)
    if warn:
        guards.append(warn)
    if amount >= money.LARGE_AMOUNT:
        guards.append(f"❗ That's a large amount ({amount:.2f}) — confirm it's correct.")

    # Mirror into the user's shared sheet, if they've connected one.
    extra = ""
    if _has_sheet(telegram_id):
        try:
            used = sheets.append_transaction(telegram_id, amount, kind, category, note)
            extra = f" → saved to your sheet ({used})"
        except Exception:  # noqa: BLE001
            extra = " (⚠️ couldn't write to your sheet — is it still shared with me?)"

    base = f"Logged: {arrow} {amount:.2f}{label}.{extra}"
    if guards:
        base += "\n" + "\n".join(guards)
    return base


def undo_last_transaction(telegram_id: int) -> str:
    row = db.last_transaction(telegram_id)
    if row is None:
        return "No transaction to undo."
    removed = db.delete_transaction(telegram_id, row.id)
    if removed is None:
        return "Couldn't undo the last transaction."
    arrow = "received" if removed.kind == "in" else "paid"
    return (f"↩️ Undone: {arrow} {removed.amount:.2f}"
            + (f" for {removed.category}" if removed.category else "")
            + ".\n(Note: the row in your sheet isn't auto-removed — delete it there if needed.)")


def edit_last_transaction(
    telegram_id: int, amount: float | None = None, kind: str | None = None,
    category: str | None = None, note: str | None = None,
) -> str:
    row = db.last_transaction(telegram_id)
    if row is None:
        return "No transaction to edit."
    if kind is not None:
        kind = "in" if str(kind).lower() in ("in", "credit", "income", "received") else "out"
    if amount is not None:
        amount = abs(float(amount))
    ok = db.update_transaction(telegram_id, row.id, amount, kind, category, note)
    if not ok:
        return "Couldn't edit the last transaction."
    new = db.last_transaction(telegram_id)
    arrow = "received" if new.kind == "in" else "paid"
    return (f"✏️ Updated last entry to: {arrow} {new.amount:.2f}"
            + (f" for {new.category}" if new.category else "") + ".")


def get_summary(telegram_id: int, days: int = 30) -> str:
    from datetime import timedelta
    since = datetime.utcnow() - timedelta(days=days)
    with db.session() as s:
        rows = s.scalars(select(db.Transaction).where(
            db.Transaction.telegram_id == telegram_id,
            db.Transaction.occurred_at >= since,
        )).all()
    if not rows:
        return f"No transactions in the last {days} days."
    inflow = sum(r.amount for r in rows if r.kind == "in")
    outflow = sum(r.amount for r in rows if r.kind == "out")
    return (f"Last {days} days: {len(rows)} transactions. "
            f"In {inflow:.2f}, Out {outflow:.2f}, Net {inflow - outflow:.2f}.")


def add_bill_account(
    telegram_id: int, name: str, due_day: int | None = None,
    statement_day: int | None = None,
) -> str:
    db.add_account(telegram_id, name, statement_day, due_day, None)
    parts = [f"Tracking '{name}'"]
    if statement_day:
        parts.append(f"statement day {statement_day}")
    if due_day:
        parts.append(f"due day {due_day} (I'll remind you before it)")
    return ". ".join(parts) + "."


def list_bill_accounts(telegram_id: int) -> str:
    accounts = db.list_accounts(telegram_id)
    if not accounts:
        return "No bills tracked yet."
    return "Tracked bills:\n" + "\n".join(
        f"• {a.name} — due day {a.due_day or '?'}" for a in accounts
    )


# =========================================================================
#  Reminders / tasks
# =========================================================================
def set_reminder(telegram_id: int, text: str, when_iso: str) -> str:
    """when_iso is a local-time ISO datetime, e.g. 2026-07-21T17:00:00."""
    local = datetime.fromisoformat(when_iso)
    if local.tzinfo is None:
        local = local.replace(tzinfo=_TZ)
    due_utc = local.astimezone(_UTC).replace(tzinfo=None)   # store naive UTC
    db.add_reminder(telegram_id, text, due_utc)
    return f"⏰ Reminder set for {local.astimezone(_TZ):%a %d %b %Y, %H:%M}: {text}"


def list_reminders(telegram_id: int) -> str:
    rows = db.list_reminders(telegram_id)
    if not rows:
        return "No pending reminders."
    lines = []
    for r in rows:
        local = r.due_at.replace(tzinfo=_UTC).astimezone(_TZ)
        lines.append(f"#{r.id} — {local:%a %d %b, %H:%M}: {r.text}")
    return "Pending reminders:\n" + "\n".join(lines)


def cancel_reminder(telegram_id: int, reminder_id: int) -> str:
    return "Reminder cancelled." if db.cancel_reminder(telegram_id, reminder_id) \
        else "No such reminder."


# =========================================================================
#  Password vault (encrypted at rest)
# =========================================================================
def save_password(telegram_id: int, name: str, secret: str, username: str | None = None) -> str:
    db.save_secret(telegram_id, name, crypto.encrypt(secret), username)
    return f"🔒 Saved credential '{name}' (encrypted)."


def get_password(telegram_id: int, name: str) -> str:
    row = db.get_secret(telegram_id, name)
    if not row:
        return f"No saved credential named '{name}'. Use 'list passwords' to see names."
    secret = crypto.decrypt(row.secret_enc)
    user_line = f"User: {row.username}\n" if row.username else ""
    return (f"🔐 {row.name}\n{user_line}Password: {secret}\n\n"
            f"⚠️ Delete this message after use — Telegram keeps chat history.")


def list_passwords(telegram_id: int) -> str:
    names = db.list_secret_names(telegram_id)
    return "Saved credentials: " + (", ".join(names) if names else "none yet.")


def delete_password(telegram_id: int, name: str) -> str:
    return "Deleted." if db.delete_secret(telegram_id, name) else f"No credential named '{name}'."


# =========================================================================
#  Google sheet / drive: connect & read (share-a-sheet model)
# =========================================================================
def sheet_setup_help(telegram_id: int) -> str:
    """Explain how to connect a sheet — the bot's email + the steps."""
    if not gservice.available_for(telegram_id):
        return "Google isn't set up on the bot yet — ask the owner to finish the service-account setup."
    email = gservice.service_account_email(telegram_id)
    return (
        "To keep your records in your own Google Sheet:\n"
        f"1. Open your Google Sheet → click Share\n"
        f"2. Add this email as Editor:\n{email}\n"
        f"3. Send me the sheet link.\n\n"
        "For saving bill photos, do the same with a Google Drive folder and send me that link too."
    )


def register_sheet(telegram_id: int, sheet_url: str) -> str:
    """Connect the user's Google Sheet (already shared with the bot's email)."""
    if not gservice.available_for(telegram_id):
        return "Google isn't set up on the bot yet — ask the owner to finish the service-account setup."
    ok, msg = sheets.register(telegram_id, sheet_url)
    return msg


def register_drive_folder(telegram_id: int, folder_url: str) -> str:
    """Connect the user's Google Drive folder for saving bill photos."""
    if not gservice.available_for(telegram_id):
        return "Google isn't set up on the bot yet — ask the owner to finish the service-account setup."
    ok, msg = drive.register(telegram_id, folder_url)
    return msg


def list_sheets(telegram_id: int) -> str:
    rows = db.list_sheets(telegram_id)
    if not rows:
        return "No Google Sheets connected yet. Send /connect and share a sheet to add one."
    default = db.default_sheet_id(telegram_id)
    lines = [f"📊 You have {len(rows)} sheet(s) connected:"]
    for r in rows:
        lines.append(f"• {r.title or 'Sheet'}" + ("  ⭐ default (entries saved here)" if r.sheet_id == default else ""))
    return "\n".join(lines)


def read_sheet(telegram_id: int, limit: int = 100, tab: str | None = None) -> str:
    """Read the user's connected sheet so the AI can reason over their data."""
    if not _has_sheet(telegram_id):
        return ("You haven't connected a sheet yet. " + sheet_setup_help(telegram_id))
    try:
        rows = sheets.read_rows(telegram_id, limit, tab)
    except Exception as e:  # noqa: BLE001
        return f"Couldn't read your sheet: {e}"
    if not rows:
        return "That tab is empty."
    return "Your sheet data (tab-separated):\n" + "\n".join("\t".join(map(str, r)) for r in rows)


def sheet_structure(telegram_id: int) -> str:
    """List every TAB in the default sheet with its column headers."""
    if not _has_sheet(telegram_id):
        return ("You haven't connected a sheet yet. " + sheet_setup_help(telegram_id))
    try:
        return ("Tabs and columns in your default sheet:\n"
                + sheets.describe_structure(telegram_id)
                + "\nUse add_sheet_row with the exact column names above.")
    except Exception as e:  # noqa: BLE001
        return f"Couldn't read your sheet structure: {e}"


def _as_dict(fields) -> dict:
    """Accept a real object or a JSON string (models sometimes send a string)."""
    if isinstance(fields, dict):
        return fields
    if isinstance(fields, str):
        import json
        try:
            parsed = json.loads(fields)
            return parsed if isinstance(parsed, dict) else {}
        except Exception:  # noqa: BLE001
            return {}
    return {}


def add_sheet_row(telegram_id: int, fields, tab: str | None = None) -> str:
    """Write one row into a specific tab, matching the tab's own column headers."""
    if not _has_sheet(telegram_id):
        return ("You haven't connected a sheet yet. " + sheet_setup_help(telegram_id))
    data = _as_dict(fields)
    if not data:
        return "No column values given — pass fields like {\"DATE\": \"...\", \"AMOUNT\": \"5000\"}."
    if tab:
        real = sheets.resolve_tab(telegram_id, tab)
        if real is None:
            try:
                available = ", ".join(sheets.list_tabs(telegram_id))
            except Exception as e:  # noqa: BLE001
                return f"Couldn't open your sheet: {e}"
            return (f"There's no tab matching '{tab}'. Tabs in this sheet: {available}. "
                    f"Pick one of these and call add_sheet_row again.")
    try:
        used, unmatched = sheets.append_mapped(telegram_id, data, tab)
    except Exception as e:  # noqa: BLE001
        return f"Couldn't write to your sheet: {e}"
    msg = f"✅ Row added to the '{used}' tab: " + ", ".join(
        f"{k}={v}" for k, v in data.items() if v not in (None, ""))
    if unmatched:
        msg += (f"\n(No column found for: {', '.join(unmatched)} — "
                f"those values were not written.)")
    return msg


def switch_sheet(telegram_id: int, name: str) -> str:
    """Make one of the connected sheets the default (where entries go)."""
    sheet_id = db.resolve_sheet(telegram_id, name)
    if not sheet_id:
        rows = db.list_sheets(telegram_id)
        names = ", ".join(r.title or "Sheet" for r in rows) or "none"
        return f"No connected sheet matches '{name}'. Connected: {names}."
    db.set_default_sheet(telegram_id, sheet_id)
    return f"✅ Entries will now go to '{name}'."


def upload_image_to_drive(telegram_id: int, filename: str | None = None) -> str:
    """Upload the image the user most recently sent to Drive and return a public link."""
    blob = _last_image.get(telegram_id)
    if not blob:
        return "I don't have a recent image from you — send the photo again."
    content, mime, default_name = blob
    link, where = drive.save_anywhere(telegram_id, filename or default_name, content, mime)
    if not link:
        return f"Couldn't upload to Drive ({where})."
    return f"📁 Uploaded to {where} (anyone with the link can view): {link}"


# =========================================================================
#  Gmail / Calendar / Docs / Drive  (personal Google login — multi-account)
# =========================================================================
def list_accounts(telegram_id: int) -> str:
    accts = db.list_google_accounts(telegram_id)
    if not accts:
        return "No Google accounts linked yet. Send /connect to add one (you can add several)."
    return "Linked Google accounts:\n" + "\n".join(f"• {a.email}" for a in accts)


def read_emails(telegram_id: int, query: str = "", count: int = 5, account: str | None = None) -> str:
    email, ask = _resolve_account(telegram_id, account)
    if ask:
        return ask
    try:
        mails = gmail.read_recent(telegram_id, email, query, count)
    except Exception as e:  # noqa: BLE001
        return f"Couldn't read email: {e}"
    if not mails:
        return f"No matching emails in {email}."
    return f"From {email}:\n\n" + "\n\n".join(
        f"From: {m['from']}\nSubject: {m['subject']}\nDate: {m['date']}\n{m['snippet']}"
        for m in mails
    )


def send_email(telegram_id: int, subject: str, body: str,
               to: str | None = None, account: str | None = None) -> str:
    email, ask = _resolve_account(telegram_id, account)
    if ask:
        return ask
    try:
        gmail.send_email(telegram_id, email, subject, body, to)
    except Exception as e:  # noqa: BLE001
        return f"Couldn't send email: {e}"
    return f"📧 Email sent from {email}{f' to {to}' if to else ' to itself'}."


def add_calendar_event(
    telegram_id: int, title: str, start_iso: str,
    end_iso: str | None = None, description: str | None = None, account: str | None = None,
) -> str:
    email, ask = _resolve_account(telegram_id, account)
    if ask:
        return ask
    try:
        link = gcal.create_event(telegram_id, email, title, start_iso, end_iso, description)
    except Exception as e:  # noqa: BLE001
        return f"Couldn't add the event: {e}"
    return f"📅 Added '{title}' to {email}. {link}"


def list_schedule(telegram_id: int, days: int = 7, account: str | None = None) -> str:
    email, ask = _resolve_account(telegram_id, account)
    if ask:
        return ask
    try:
        events = gcal.list_events(telegram_id, email, days)
    except Exception as e:  # noqa: BLE001
        return f"Couldn't read your calendar: {e}"
    if not events:
        return f"Nothing scheduled in {email} in the next {days} days."
    return f"Upcoming ({email}):\n" + "\n".join(f"• {e['start']}: {e['summary']}" for e in events)


def create_document(telegram_id: int, title: str, content: str = "", account: str | None = None) -> str:
    email, ask = _resolve_account(telegram_id, account)
    if ask:
        return ask
    try:
        url = gdocs.create_doc(telegram_id, email, title, content)
    except Exception as e:  # noqa: BLE001
        return f"Couldn't create the doc: {e}"
    return f"📄 Created '{title}' in {email}: {url}"


def list_drive_files(telegram_id: int, query: str = "", account: str | None = None) -> str:
    email, ask = _resolve_account(telegram_id, account)
    if ask:
        return ask
    try:
        svc = goauth.drive(telegram_id, email)
        q = f"name contains '{query}'" if query else None
        resp = svc.files().list(
            q=q, pageSize=15, orderBy="modifiedTime desc",
            fields="files(name,mimeType,webViewLink)",
        ).execute()
    except Exception as e:  # noqa: BLE001
        return f"Couldn't read Drive: {e}"
    files = resp.get("files", [])
    if not files:
        return f"No matching Drive files in {email}."
    return f"Drive files ({email}):\n" + "\n".join(
        f"• {f['name']} — {f.get('webViewLink', '')}" for f in files)


def analyze_statement(telegram_id: int, gmail_query: str,
                      pdf_password: str | None = None, account: str | None = None) -> str:
    """Fetch the latest statement email + read its PDF so the AI can summarise it."""
    email, ask = _resolve_account(telegram_id, account)
    if ask:
        return ask
    try:
        result = gmail.fetch_latest_statement(telegram_id, email, gmail_query)
    except Exception as e:  # noqa: BLE001
        return f"Couldn't fetch the statement: {e}"
    if not result:
        return "No matching statement email (with an attachment) found recently."
    chunks = [f"Statement email subject: {result['subject']}"]
    read_any = False
    for att in result["attachments"]:
        fn = att["filename"]
        if fn.lower().endswith(".pdf") or "pdf" in att["mime"].lower():
            try:
                chunks.append(f"--- {fn} ---\n{pdf.extract_text(att['content'], password=pdf_password)}")
                read_any = True
            except pdf.EncryptedPDF:
                return (f"The statement '{fn}' is password-protected. Tell me the PDF "
                        f"password (or save it in your vault) and I'll read it.")
            except Exception as e:  # noqa: BLE001
                chunks.append(f"--- {fn} --- (couldn't read: {e})")
    if not read_any:
        return "Found the statement but couldn't extract readable text."
    return "\n\n".join(chunks)


# =========================================================================
#  Non-Google mailbox (IMAP/SMTP — Migadu, Zoho, custom hosts)
# =========================================================================
def _resolve_mailbox(telegram_id: int, account: str | None):
    accts = db.list_mail_accounts(telegram_id)
    if not accts:
        return None, "No email mailbox connected yet. Add one with /addmail (works for Migadu, Zoho, custom hosts)."
    acct = db.get_mail_account(telegram_id, account)
    if acct is None:
        return None, ("No mailbox matches that. Connected: " + ", ".join(a.email for a in accts))
    acct.password = crypto.decrypt(acct.password_enc)
    return acct, None


def check_mailbox(telegram_id: int, count: int = 5, account: str | None = None) -> str:
    """Read recent mail from a password-connected mailbox (Migadu etc.)."""
    acct, err = _resolve_mailbox(telegram_id, account)
    if err:
        return err
    try:
        mails = mailbox.check_inbox(acct, max(1, min(count, 15)))
    except Exception as e:  # noqa: BLE001
        return f"Couldn't check {acct.email}: {e}"
    if not mails:
        return f"No mail in {acct.email}."
    return f"📬 Latest in {acct.email}:\n\n" + "\n\n".join(
        f"From: {m['from']}\nSubject: {m['subject']}\n{m['date']}" for m in mails)


def send_from_mailbox(telegram_id: int, to: str, subject: str, body: str,
                      account: str | None = None) -> str:
    """Send an email from a password-connected mailbox (Migadu etc.)."""
    acct, err = _resolve_mailbox(telegram_id, account)
    if err:
        return err
    try:
        mailbox.send_mail(acct, to, subject, body)
    except Exception as e:  # noqa: BLE001
        return f"Couldn't send from {acct.email}: {e}"
    return f"📧 Sent from {acct.email} to {to}."


def list_mailboxes(telegram_id: int) -> str:
    accts = db.list_mail_accounts(telegram_id)
    if not accts:
        return "No email mailboxes connected. Add one with /addmail."
    dflt = db.get_mail_account(telegram_id)
    return "📬 Connected mailboxes:\n" + "\n".join(
        f"• {a.email}" + ("  ⭐ default" if dflt and a.email == dflt.email else "") for a in accts)


# =========================================================================
#  Registry + schemas
# =========================================================================
TOOLS: dict[str, callable] = {
    "log_transaction": log_transaction,
    "undo_last_transaction": undo_last_transaction,
    "edit_last_transaction": edit_last_transaction,
    "get_summary": get_summary,
    "add_bill_account": add_bill_account,
    "list_bill_accounts": list_bill_accounts,
    "set_reminder": set_reminder,
    "list_reminders": list_reminders,
    "cancel_reminder": cancel_reminder,
    "save_password": save_password,
    "get_password": get_password,
    "list_passwords": list_passwords,
    "delete_password": delete_password,
    "sheet_setup_help": sheet_setup_help,
    "register_sheet": register_sheet,
    "register_drive_folder": register_drive_folder,
    "list_sheets": list_sheets,
    "read_sheet": read_sheet,
    "sheet_structure": sheet_structure,
    "add_sheet_row": add_sheet_row,
    "switch_sheet": switch_sheet,
    "upload_image_to_drive": upload_image_to_drive,
    "list_accounts": list_accounts,
    "read_emails": read_emails,
    "send_email": send_email,
    "add_calendar_event": add_calendar_event,
    "list_schedule": list_schedule,
    "create_document": create_document,
    "list_drive_files": list_drive_files,
    "analyze_statement": analyze_statement,
    "check_mailbox": check_mailbox,
    "send_from_mailbox": send_from_mailbox,
    "list_mailboxes": list_mailboxes,
}


def _fn(name, desc, props, required=None):
    return {"type": "function", "function": {
        "name": name, "description": desc,
        "parameters": {"type": "object", "properties": props, "required": required or []},
    }}


SCHEMAS: list[dict] = [
    _fn("log_transaction", "Record a money transaction the user reports (payment made or money received).",
        {"amount": {"type": "number", "description": "Positive amount."},
         "kind": {"type": "string", "enum": ["in", "out"], "description": "'in' if received, 'out' if paid. Default 'out'."},
         "category": {"type": "string", "description": "e.g. electricity, rent, salary."},
         "note": {"type": "string"}}, ["amount"]),
    _fn("undo_last_transaction", "Delete/undo the user's most recent transaction (use when they say it was wrong or a mistake).", {}),
    _fn("edit_last_transaction", "Correct the user's most recent transaction. Only pass the fields that change.",
        {"amount": {"type": "number"},
         "kind": {"type": "string", "enum": ["in", "out"]},
         "category": {"type": "string"},
         "note": {"type": "string"}}),
    _fn("get_summary", "Summarise recent transactions (totals in/out/net).",
        {"days": {"type": "integer", "description": "Look-back window. Default 30."}}),
    _fn("add_bill_account", "Track a bill/card to get a reminder before its due date.",
        {"name": {"type": "string"},
         "due_day": {"type": "integer", "description": "Day of month the bill is due (1-31)."},
         "statement_day": {"type": "integer", "description": "Day of month the statement arrives (1-31). Optional."}},
        ["name"]),
    _fn("list_bill_accounts", "List tracked bills.", {}),

    _fn("set_reminder", "Set a time-based reminder. Compute the exact local datetime from the user's words using the current time given to you.",
        {"text": {"type": "string", "description": "What to remind about."},
         "when_iso": {"type": "string", "description": "Local ISO datetime, e.g. 2026-07-21T17:00:00."}},
        ["text", "when_iso"]),
    _fn("list_reminders", "List the user's pending reminders.", {}),
    _fn("cancel_reminder", "Cancel a reminder by its id.",
        {"reminder_id": {"type": "integer"}}, ["reminder_id"]),

    _fn("save_password", "Save/update a password or credential in the user's encrypted vault.",
        {"name": {"type": "string", "description": "Label, e.g. 'gmail', 'wifi'."},
         "secret": {"type": "string", "description": "The password/secret value."},
         "username": {"type": "string", "description": "Optional username/login."}},
        ["name", "secret"]),
    _fn("get_password", "Retrieve a saved credential by name.",
        {"name": {"type": "string"}}, ["name"]),
    _fn("list_passwords", "List the names of saved credentials (not the values).", {}),
    _fn("delete_password", "Delete a saved credential by name.",
        {"name": {"type": "string"}}, ["name"]),

    _fn("sheet_setup_help", "Explain how the user connects their Google Sheet/Drive folder (gives the bot's share email + steps). Use when they ask how to connect a sheet.", {}),
    _fn("register_sheet", "Connect a Google Sheet the user has shared with the bot. Use when they send a Google Sheets link.",
        {"sheet_url": {"type": "string", "description": "The Google Sheets URL or id."}}, ["sheet_url"]),
    _fn("register_drive_folder", "Connect a Google Drive folder for saving bill photos. Use when they send a Drive folder link.",
        {"folder_url": {"type": "string", "description": "The Google Drive folder URL or id."}}, ["folder_url"]),
    _fn("list_sheets", "List how many Google Sheets the user has connected and which is the default.", {}),
    _fn("read_sheet", "Read a tab of the user's default connected sheet so you can answer questions about their data (spending, totals, history).",
        {"limit": {"type": "integer", "description": "How many recent rows. Default 100."},
         "tab": {"type": "string", "description": "Which tab to read, e.g. 'EXPENSES'. Omit for the first tab."}}),
    _fn("sheet_structure", "List every TAB in the user's sheet and that tab's column headers. Call this FIRST whenever you need to write a row and don't already know the tabs/columns.", {}),
    _fn("add_sheet_row", "Write ONE row into a specific tab of the user's sheet, filling that tab's own columns. Use this (not log_transaction) when the user names a tab or the sheet has custom columns like TRANSFER TO / TRANSFER FROM / REASON / IMAGES.",
        {"fields": {"type": "object",
                    "description": "Column name -> value, using the tab's real headers, e.g. {\"DATE\":\"27/07/2026\",\"TRANSFER TO\":\"PAMPOSH\",\"AMOUNT\":\"5000\",\"TRANSFER FROM\":\"PINKY YES ACC.\",\"REASON\":\"WASHROOM CLEANER\",\"PAYMENT MODE\":\"PAYTM UPI\",\"IMAGES\":\"https://drive.google.com/...\"}",
                    "additionalProperties": {"type": "string"}},
         "tab": {"type": "string", "description": "Tab name, e.g. 'EXPENSES'. Loose names like 'expense' are matched automatically."}},
        ["fields"]),
    _fn("switch_sheet", "Make one of the user's connected sheets the default one that entries go into.",
        {"name": {"type": "string", "description": "Part of the sheet's title, e.g. 'expenses'."}}, ["name"]),
    _fn("upload_image_to_drive", "Upload the image the user most recently sent to their Drive and get back a public 'anyone with the link' URL (for putting in a sheet's IMAGES column).",
        {"filename": {"type": "string", "description": "Optional file name."}}),

    _fn("list_accounts", "List the user's linked Google accounts (emails).", {}),

    _fn("read_emails", "Read/summarise recent Gmail from a linked account. If the user has multiple accounts and didn't say which, the tool will ask.",
        {"query": {"type": "string", "description": "Gmail search, e.g. 'from:boss', 'is:unread', 'invoice'. Empty = inbox."},
         "count": {"type": "integer", "description": "How many. Default 5, max 15."},
         "account": {"type": "string", "description": "Which linked account (email or part of it). Optional."}}),
    _fn("send_email", "Send an email from a linked Google account. Defaults to sending to that account itself if no recipient.",
        {"subject": {"type": "string"}, "body": {"type": "string"},
         "to": {"type": "string", "description": "Recipient email. Optional."},
         "account": {"type": "string", "description": "Which linked account to send from. Optional."}},
        ["subject", "body"]),
    _fn("add_calendar_event", "Add an event to a linked account's Google Calendar. Compute ISO datetimes from the current time.",
        {"title": {"type": "string"},
         "start_iso": {"type": "string", "description": "Local ISO start, e.g. 2026-07-23T17:00:00."},
         "end_iso": {"type": "string", "description": "Local ISO end. Optional (defaults +1h)."},
         "description": {"type": "string"},
         "account": {"type": "string", "description": "Which linked account. Optional."}},
        ["title", "start_iso"]),
    _fn("list_schedule", "List upcoming Google Calendar events from a linked account.",
        {"days": {"type": "integer", "description": "How many days ahead. Default 7."},
         "account": {"type": "string", "description": "Which linked account. Optional."}}),
    _fn("create_document", "Create a Google Doc in a linked account.",
        {"title": {"type": "string"}, "content": {"type": "string"},
         "account": {"type": "string", "description": "Which linked account. Optional."}}, ["title"]),
    _fn("list_drive_files", "List/search files in a linked account's Google Drive.",
        {"query": {"type": "string", "description": "Filter by name. Empty = most recent."},
         "account": {"type": "string", "description": "Which linked account. Optional."}}),
    _fn("analyze_statement", "Fetch the latest bank/card statement from a linked Gmail and read the PDF so you can summarise it (spend, due amount, due date).",
        {"gmail_query": {"type": "string", "description": "Gmail search, e.g. 'from:hdfcbank statement'."},
         "pdf_password": {"type": "string", "description": "Password if the PDF is protected. Optional."},
         "account": {"type": "string", "description": "Which linked account. Optional."}},
        ["gmail_query"]),

    _fn("check_mailbox", "Read recent email from a NON-Google mailbox the user connected with a password (Migadu, Zoho, custom IMAP). Use this (not read_emails) for those. To add one, tell them to use /addmail.",
        {"count": {"type": "integer", "description": "How many recent messages. Default 5."},
         "account": {"type": "string", "description": "Which mailbox (email or part of it) if they have several. Optional."}}),
    _fn("send_from_mailbox", "Send an email from a NON-Google mailbox connected with a password (Migadu etc.).",
        {"to": {"type": "string"}, "subject": {"type": "string"}, "body": {"type": "string"},
         "account": {"type": "string", "description": "Which mailbox to send from. Optional."}},
        ["to", "subject", "body"]),
    _fn("list_mailboxes", "List the non-Google mailboxes (IMAP) the user has connected.", {}),
]
