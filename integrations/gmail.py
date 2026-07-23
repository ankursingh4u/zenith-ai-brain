"""Per-user Gmail: send notifications, and fetch statement emails + attachments."""
from __future__ import annotations

import base64
from datetime import datetime, timedelta
from email.mime.text import MIMEText

from integrations import client


# --- Sending --------------------------------------------------------------
def send_email(telegram_id: int, account: str, subject: str, body: str,
               to: str | None = None) -> None:
    """Send a plain-text email from `account` (to that account itself by default)."""
    to = to or account
    msg = MIMEText(body, _charset="utf-8")
    msg["to"] = to
    msg["subject"] = subject
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    client.gmail(telegram_id, account).users().messages().send(
        userId="me", body={"raw": raw}
    ).execute()


# --- Reading email --------------------------------------------------------
def read_recent(telegram_id: int, account: str, query: str = "", count: int = 5) -> list[dict]:
    """Return recent emails matching an optional Gmail query.

    Each item: {from, subject, date, snippet}. The AI summarises from these.
    """
    svc = client.gmail(telegram_id, account)
    listed = svc.users().messages().list(
        userId="me", q=query or "in:inbox", maxResults=max(1, min(count, 15))
    ).execute()
    out = []
    for m in listed.get("messages", []):
        msg = svc.users().messages().get(
            userId="me", id=m["id"], format="metadata",
            metadataHeaders=["From", "Subject", "Date"],
        ).execute()
        h = {x["name"].lower(): x["value"] for x in msg["payload"].get("headers", [])}
        out.append({
            "from": h.get("from", ""),
            "subject": h.get("subject", "(no subject)"),
            "date": h.get("date", ""),
            "snippet": msg.get("snippet", ""),
        })
    return out


# --- Fetching statements --------------------------------------------------
def _walk_parts(part, out):
    """Collect (filename, attachmentId, mimeType) for every attachment part."""
    if part.get("filename") and part.get("body", {}).get("attachmentId"):
        out.append((part["filename"], part["body"]["attachmentId"], part.get("mimeType", "")))
    for sub in part.get("parts", []) or []:
        _walk_parts(sub, out)


def fetch_latest_statement(telegram_id: int, account: str, query: str,
                           within_days: int = 40) -> dict | None:
    """Find the newest email matching `query` and pull its attachments.

    Returns {'subject', 'attachments': [{'filename','mime','content'}]} or None.
    """
    svc = client.gmail(telegram_id, account)
    after = (datetime.now() - timedelta(days=within_days)).strftime("%Y/%m/%d")
    full_query = f"{query} has:attachment after:{after}"

    listed = svc.users().messages().list(
        userId="me", q=full_query, maxResults=1
    ).execute()
    messages = listed.get("messages", [])
    if not messages:
        return None

    msg = svc.users().messages().get(
        userId="me", id=messages[0]["id"], format="full"
    ).execute()

    headers = {h["name"].lower(): h["value"] for h in msg["payload"].get("headers", [])}
    subject = headers.get("subject", "(no subject)")

    parts: list = []
    _walk_parts(msg["payload"], parts)

    attachments = []
    for filename, att_id, mime in parts:
        att = svc.users().messages().attachments().get(
            userId="me", messageId=msg["id"], id=att_id
        ).execute()
        content = base64.urlsafe_b64decode(att["data"])
        attachments.append({"filename": filename, "mime": mime or "application/octet-stream",
                            "content": content})

    return {"subject": subject, "attachments": attachments}
