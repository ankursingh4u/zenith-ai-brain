"""Per-user Google Calendar: create and list events."""
from __future__ import annotations

from datetime import datetime, timedelta

from integrations import client


def create_event(
    telegram_id: int, account: str, title: str, start_iso: str,
    end_iso: str | None = None, description: str | None = None,
) -> str:
    """Create an event. Times are ISO 8601 (e.g. 2026-07-21T17:00:00). Returns its link."""
    svc = client.calendar(telegram_id, account)
    start = datetime.fromisoformat(start_iso)
    end = datetime.fromisoformat(end_iso) if end_iso else start + timedelta(hours=1)
    body = {
        "summary": title,
        "description": description or "",
        "start": {"dateTime": start.isoformat()},
        "end": {"dateTime": end.isoformat()},
    }
    ev = svc.events().insert(calendarId="primary", body=body).execute()
    return ev.get("htmlLink", "created")


def list_events(telegram_id: int, account: str, days: int = 7) -> list[dict]:
    """Upcoming events in the next `days` days: [{summary, start}]."""
    svc = client.calendar(telegram_id, account)
    now = datetime.now().astimezone()
    later = now + timedelta(days=days)
    result = svc.events().list(
        calendarId="primary",
        timeMin=now.isoformat(),
        timeMax=later.isoformat(),
        singleEvents=True,
        orderBy="startTime",
        maxResults=20,
    ).execute()
    out = []
    for e in result.get("items", []):
        start = e.get("start", {}).get("dateTime") or e.get("start", {}).get("date", "")
        out.append({"summary": e.get("summary", "(no title)"), "start": start})
    return out
