"""Generic IMAP/SMTP mailbox access (Migadu, Zoho, Fastmail, custom hosts, ...).

Read recent mail over IMAP, send over SMTP. Credentials are passed in already
decrypted by the caller; this module never touches the database or the AI.
"""
from __future__ import annotations

import email
import imaplib
import smtplib
import ssl
from email.header import decode_header, make_header
from email.mime.text import MIMEText

# Known providers → (imap_host, imap_port, smtp_host, smtp_port).
PROVIDERS = {
    "migadu":   ("imap.migadu.com", 993, "smtp.migadu.com", 465),
    "zoho":     ("imap.zoho.com", 993, "smtp.zoho.com", 465),
    "fastmail": ("imap.fastmail.com", 993, "smtp.fastmail.com", 465),
    "outlook":  ("outlook.office365.com", 993, "smtp.office365.com", 587),
    "yahoo":    ("imap.mail.yahoo.com", 993, "smtp.mail.yahoo.com", 465),
    "icloud":   ("imap.mail.me.com", 993, "smtp.mail.me.com", 587),
}


def resolve_hosts(email_addr: str, provider_or_imap: str | None,
                  smtp: str | None) -> tuple[str, int, str, int]:
    """Work out IMAP/SMTP hosts from a provider keyword, explicit hosts, or the domain."""
    if provider_or_imap and provider_or_imap.lower() in PROVIDERS:
        return PROVIDERS[provider_or_imap.lower()]
    if provider_or_imap and "." in provider_or_imap:      # explicit imap host given
        imap_host = provider_or_imap
        domain = email_addr.split("@")[-1]
        smtp_host = smtp or provider_or_imap.replace("imap.", "smtp.", 1)
        if smtp_host == imap_host:
            smtp_host = f"smtp.{domain}"
        return imap_host, 993, smtp_host, 465
    domain = email_addr.split("@")[-1]                    # last resort: guess from domain
    return f"imap.{domain}", 993, f"smtp.{domain}", 465


def _dec(v: str | None) -> str:
    if not v:
        return ""
    try:
        return str(make_header(decode_header(v)))
    except Exception:  # noqa: BLE001
        return v


def check_inbox(acct, count: int = 5) -> list[dict]:
    """Return the newest `count` messages as [{from, subject, date, snippet}]."""
    ctx = ssl.create_default_context()
    M = imaplib.IMAP4_SSL(acct.imap_host, acct.imap_port, ssl_context=ctx)
    try:
        M.login(acct.email, acct.password)
        M.select("INBOX")
        typ, data = M.search(None, "ALL")
        ids = data[0].split()
        out = []
        for msg_id in reversed(ids[-count:]):
            typ, msg_data = M.fetch(msg_id, "(RFC822.HEADER BODY.PEEK[TEXT]<0.400>)")
            raw = b"".join(part[1] for part in msg_data if isinstance(part, tuple))
            msg = email.message_from_bytes(raw)
            out.append({
                "from": _dec(msg.get("From")),
                "subject": _dec(msg.get("Subject")) or "(no subject)",
                "date": msg.get("Date", ""),
                "snippet": "",
            })
        return out
    finally:
        try:
            M.logout()
        except Exception:  # noqa: BLE001
            pass


def send_mail(acct, to: str, subject: str, body: str) -> None:
    msg = MIMEText(body, _charset="utf-8")
    msg["From"] = acct.email
    msg["To"] = to
    msg["Subject"] = subject
    ctx = ssl.create_default_context()
    if int(acct.smtp_port) == 465:
        with smtplib.SMTP_SSL(acct.smtp_host, acct.smtp_port, context=ctx) as s:
            s.login(acct.email, acct.password)
            s.send_message(msg)
    else:
        with smtplib.SMTP(acct.smtp_host, acct.smtp_port) as s:
            s.starttls(context=ctx)
            s.login(acct.email, acct.password)
            s.send_message(msg)
