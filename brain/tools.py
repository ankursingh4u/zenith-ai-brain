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
import json
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import select

import config
import crypto
import db
from brain import memory, money, notes, web

try:
    from rapidfuzz import fuzz
except ImportError:  # pragma: no cover — exact matching still works without it
    fuzz = None
from integrations import calendar as gcal
from integrations import client as goauth
from integrations import docs as gdocs
from integrations import drive, gmail, gservice, mailbox, pdf, sheets
from integrations import vault as vaultmod

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

_DEFAULT_TZ = ZoneInfo(config.TIMEZONE)


def _tz(telegram_id: int | None = None) -> ZoneInfo:
    """THIS user's timezone. The config value is only a fallback, never a claim.

    Every user of this bot can live somewhere different, so "7am" has to mean
    7am where they are. A single module-level timezone silently gave everyone
    the server's clock.
    """
    if telegram_id is None:
        return _DEFAULT_TZ
    try:
        return ZoneInfo(db.get_locale(telegram_id)["timezone"])
    except Exception:  # noqa: BLE001 — an unknown tz must not break the turn
        return _DEFAULT_TZ
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
_REPEATS = {"daily", "weekdays", "weekends", "weekly"}


_WEEKDAY_NAMES = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


def _rem_local(r, tz=None):
    """A stored reminder's time in the owning user's timezone."""
    return r.due_at.replace(tzinfo=_UTC).astimezone(tz or _DEFAULT_TZ)


def _repeat_days(local, repeat: str | None) -> set[int] | None:
    """Which weekdays this reminder can land on. None = a one-off on a fixed date."""
    rep = (repeat or "").strip().lower()
    if rep == "daily":
        return {0, 1, 2, 3, 4, 5, 6}
    if rep == "weekdays":
        return {0, 1, 2, 3, 4}
    if rep == "weekends":
        return {5, 6}
    if rep == "weekly":
        return {local.weekday()}
    return None


def _can_share_a_day(a_local, a_rep, b_local, b_rep) -> bool:
    """Could these two ever fire on the SAME day? Two one-offs need the same date."""
    a_days, b_days = _repeat_days(a_local, a_rep), _repeat_days(b_local, b_rep)
    if a_days is None and b_days is None:
        return a_local.date() == b_local.date()
    if a_days is None:
        return a_local.weekday() in b_days
    if b_days is None:
        return b_local.weekday() in a_days
    return bool(a_days & b_days)


def _clashes(telegram_id: int, local, repeat: str | None, window_min: int = 30,
             skip_id: int | None = None) -> list[tuple]:
    """Existing reminders within `window_min` of this one, on a day it could share.

    Deterministic here on purpose — the assistant decides what to DO about a
    clash, but it must never have to guess whether one exists.
    """
    out = []
    tz = _tz(telegram_id)
    mins = local.hour * 60 + local.minute
    for r in db.list_reminders(telegram_id):
        if skip_id is not None and r.id == skip_id:
            continue
        r_local = _rem_local(r, tz)
        if not _can_share_a_day(local, repeat, r_local, getattr(r, "repeat", None)):
            continue
        r_mins = r_local.hour * 60 + r_local.minute
        gap = abs(mins - r_mins)
        gap = min(gap, 1440 - gap)               # 23:50 and 00:05 are 15 apart
        if gap <= window_min:
            out.append((gap, r, r_local))
    return sorted(out, key=lambda x: x[0])


def check_time_free(telegram_id: int, when_iso: str, repeat: str | None = None,
                    window_min: int = 30) -> str:
    """Is this slot free? Call BEFORE promising a new time block."""
    try:
        local = datetime.fromisoformat(when_iso)
    except ValueError:
        return "I couldn't read that time. Give it as 2026-07-31T19:30:00."
    if local.tzinfo is None:
        local = local.replace(tzinfo=_tz(telegram_id))
    hits = _clashes(telegram_id, local, repeat, int(window_min or 30))
    if not hits:
        return (f"✅ {local:%H:%M} looks free — nothing else within "
                f"{window_min} minutes of it.")
    lines = []
    for gap, r, r_local in hits[:6]:
        every = f" 🔁 {r.repeat}" if getattr(r, "repeat", None) else f" ({r_local:%a %d %b})"
        lines.append(f"• {r_local:%H:%M}{every} — {r.text} ({gap} min away)")
    return (f"⚠️ {local:%H:%M} clashes with {len(hits)} thing(s):\n"
            + "\n".join(lines)
            + "\n\nPick a different time, move the existing one, or say it's fine to overlap.")


def day_plan(telegram_id: int, day: str = "today") -> str:
    """The timeline for one day — every reminder that fires, in order.

    This is what makes 'where does 2 hours of dev actually fit?' answerable
    instead of guessed.
    """
    tz = _tz(telegram_id)
    now_local = datetime.now(tz)
    want = (day or "today").strip().lower()
    target = now_local.date()
    if want == "tomorrow":
        target = (now_local + timedelta(days=1)).date()
    elif want in [d.lower() for d in _WEEKDAY_NAMES] or want in (
            "monday", "tuesday", "wednesday", "thursday", "friday",
            "saturday", "sunday"):
        wanted = [d.lower() for d in _WEEKDAY_NAMES].index(want[:3])
        ahead = (wanted - now_local.weekday()) % 7
        target = (now_local + timedelta(days=ahead)).date()

    rows = []
    for r in db.list_reminders(telegram_id):
        r_local = _rem_local(r, tz)
        days = _repeat_days(r_local, getattr(r, "repeat", None))
        fires = (r_local.date() == target) if days is None else (target.weekday() in days)
        if fires:
            rows.append((r_local.hour * 60 + r_local.minute, r_local, r))
    rows.sort(key=lambda x: x[0])

    head = f"📅 {target:%A %d %b}"
    if not rows:
        return head + " — nothing scheduled. The whole day is free."
    lines, prev = [], None
    for mins, r_local, r in rows:
        if prev is not None and mins - prev >= 60:
            lines.append(f"    ({(mins - prev) // 60}h {(mins - prev) % 60}m free)")
        every = f" 🔁 {r.repeat}" if getattr(r, "repeat", None) else ""
        lines.append(f"{r_local:%H:%M} — {r.text}{every}")
        prev = mins
    return head + f" — {len(rows)} block(s):\n" + "\n".join(lines)


def plan_gaps(telegram_id: int) -> str:
    """Audit the plan for real holes: no gate, empty track, stalled, overdue, clashing.

    Structural problems are found here in code so the assistant reports facts
    rather than an impression.
    """
    tz = _tz(telegram_id)
    now_local = datetime.now(tz)
    now_utc = now_local.astimezone(_UTC).replace(tzinfo=None)
    problems: list[str] = []

    tops = db.tracks(telegram_id)
    if not tops:
        return ("No plan stored yet, so there's nothing to audit. Send me your "
                "roadmap and I'll build the tree.")

    for t in tops:
        kids = db.children(telegram_id, t.id)
        if not kids:
            problems.append(f"🕳 Track '{t.title}' is empty — no phases under it.")
        open_kids = [k for k in kids if k.status == "open" and k.kind != "habit"]
        if kids and not open_kids:
            problems.append(f"🏁 Track '{t.title}' has nothing open — finished, or it needs its next phase.")

    no_gate, no_target, stalled, dupes = [], [], [], []
    seen_titles: dict[str, str] = {}
    for t in tops:
        for k in db.children(telegram_id, t.id):
            if k.kind == "habit":
                continue
            key = (k.title or "").strip().lower()
            if key in seen_titles:
                dupes.append(f"'{k.title}' appears in both {seen_titles[key]} and {t.title}")
            else:
                seen_titles[key] = t.title
            if k.status != "open":
                continue
            if not (k.gate or "").strip():
                no_gate.append(f"{t.title} / {k.title}")
            elif not k.target and not db.children(telegram_id, k.id):
                no_target.append(f"{t.title} / {k.title}")
    if no_gate:
        problems.append("🚧 No gate — you can't tell when these are done:\n   "
                        + "\n   ".join(no_gate[:8]))
    if no_target:
        problems.append("🔢 No countable target (fine if the gate is a demonstrated "
                        "skill, worth adding if it's volume work):\n   "
                        + "\n   ".join(no_target[:6]))
    if dupes:
        problems.append("👯 Same thing in two places:\n   " + "\n   ".join(dupes[:5]))

    # The phase in play in each track, sitting at zero — the real "stalled" signal.
    for t in tops:
        open_kids = [k for k in db.children(telegram_id, t.id)
                     if k.status == "open" and k.kind != "habit"]
        if not open_kids:
            continue
        cur = open_kids[0]
        age_days = (now_utc - cur.created_at).days if cur.created_at else 0
        if age_days >= 14 and not (cur.progress or 0):
            stalled.append(f"{t.title} / {cur.title} — {age_days} days, zero progress")
    if stalled:
        problems.append("🧊 Stalled:\n   " + "\n   ".join(stalled))

    overdue = db.list_tasks(telegram_id, "open", due_before=now_utc)
    if overdue:
        problems.append(f"⏳ Overdue ({len(overdue)}):\n   "
                        + "\n   ".join(f"{o.title}" for o in overdue[:6]))

    cold = []
    for h in db.habits(telegram_id):
        if h.last_done_at is None:
            cold.append(f"{h.title} — never ticked off")
        else:
            days = (now_utc.date() - h.last_done_at.date()).days
            limit = 7 if (h.recur or "").startswith("week") else 3
            if days > limit:
                cold.append(f"{h.title} — {days} days ago (streak {h.streak or 0} is at risk)")
    if cold:
        problems.append("🔁 Habits gone cold:\n   " + "\n   ".join(cold))

    reminders = db.list_reminders(telegram_id)
    reported: set[frozenset] = set()
    clash_lines = []
    for r in reminders:
        r_local = _rem_local(r, tz)
        # Tighter than the insert warning: back-to-back blocks 20 minutes apart
        # are a routine, not a conflict. Only real overlaps belong in an audit.
        for gap, other, o_local in _clashes(
                telegram_id, r_local, getattr(r, "repeat", None), 15, skip_id=r.id):
            pair = frozenset((r.id, other.id))
            if pair in reported:
                continue
            reported.add(pair)
            clash_lines.append(f"{r_local:%H:%M} '{r.text}' vs {o_local:%H:%M} "
                               f"'{other.text}' — {gap} min apart")
    if clash_lines:
        problems.append("⏰ Reminders on top of each other:\n   "
                        + "\n   ".join(clash_lines[:6]))

    if not problems:
        return ("✅ No structural gaps: every open phase has a gate, nothing is "
                "stalled or overdue, habits are warm, no reminder clashes.")
    return "Gaps in your plan:\n\n" + "\n\n".join(problems)


def set_reminder(telegram_id: int, text: str, when_iso: str,
                 repeat: str | None = None) -> str:
    """when_iso is a local-time ISO datetime. `repeat` makes it recur."""
    local = datetime.fromisoformat(when_iso)
    if local.tzinfo is None:
        local = local.replace(tzinfo=_tz(telegram_id))
    repeat = (repeat or "").strip().lower() or None
    if repeat and repeat not in _REPEATS:
        repeat = "daily" if "day" in repeat else "weekly"
    # A recurring reminder given a past time means "start from the next one".
    if repeat:
        from datetime import timedelta
        step = timedelta(weeks=1) if repeat == "weekly" else timedelta(days=1)
        while local <= datetime.now(_tz(telegram_id)):
            local += step
        if repeat == "weekdays":
            while local.weekday() >= 5:
                local += timedelta(days=1)
        elif repeat == "weekends":
            while local.weekday() < 5:
                local += timedelta(days=1)
    # Look for a clash BEFORE inserting, so the warning describes the old blocks
    # rather than the one we just added.
    hits = _clashes(telegram_id, local, repeat)
    due_utc = local.astimezone(_UTC).replace(tzinfo=None)   # store naive UTC
    db.add_reminder(telegram_id, text, due_utc, repeat)
    when = f"every day at {local:%H:%M}" if repeat == "daily" else (
        f"every weekday at {local:%H:%M}" if repeat == "weekdays" else (
            f"every weekend day at {local:%H:%M}" if repeat == "weekends" else (
                f"every {local:%A} at {local:%H:%M}" if repeat == "weekly"
                else f"{local:%a %d %b %Y, %H:%M}")))
    out = f"⏰ Reminder set for {when}: {text}"
    if hits:
        # Set anyway — the user's time is theirs — but never silently double-book.
        # The instruction lives in the TOOL RESULT, not only in the system
        # prompt: a summarising model otherwise replies "done, reminder set"
        # and silently drops the warning, which defeats detecting it at all.
        out += (f"\n\n⚠️ CLASH — REPORT THIS TO THE USER. Do not answer with just "
                f"'done'. This sits on top of {len(hits)} existing block(s):\n"
                + "\n".join(
                    f"• {o_local:%H:%M}"
                    + (f" 🔁 {o.repeat}" if getattr(o, "repeat", None) else "")
                    + f" — {o.text} ({gap} min away)"
                    for gap, o, o_local in hits[:4])
                + "\nName what it collides with, then offer to move either one.")
    return out


def list_reminders(telegram_id: int) -> str:
    rows = db.list_reminders(telegram_id)
    if not rows:
        return "No pending reminders."
    tz = _tz(telegram_id)
    lines = []
    for r in rows:
        local = r.due_at.replace(tzinfo=_UTC).astimezone(tz)
        every = f" 🔁 {r.repeat}" if getattr(r, "repeat", None) else ""
        lines.append(f"#{r.id} — {local:%a %d %b, %H:%M}{every}: {r.text}")
    return "Pending reminders:\n" + "\n".join(lines)


def cancel_reminder(telegram_id: int, reminder_id: int | None = None,
                    match: str | None = None) -> str:
    """Cancel a reminder by id or by words from it."""
    reminder_id, problem = _pick_reminder(telegram_id, reminder_id, match)
    if problem:
        return problem
    return ("Reminder cancelled." if db.cancel_reminder(telegram_id, reminder_id)
            else "No such reminder.")


# =========================================================================
#  Tasks — open work that has no required time (unlike a reminder)
# =========================================================================
_PRIORITY_LABEL = {1: "🔴", 2: "🟡", 3: "⚪"}


def _due_utc(when_iso: str | None, tz=None) -> datetime | None:
    """Local ISO -> naive UTC, using the OWNING user's timezone."""
    if not when_iso:
        return None
    try:
        local = datetime.fromisoformat(when_iso)
    except ValueError:
        return None
    if local.tzinfo is None:
        local = local.replace(tzinfo=tz or _DEFAULT_TZ)
    return local.astimezone(_UTC).replace(tzinfo=None)


def _task_line(t, tz=None) -> str:
    mark = _PRIORITY_LABEL.get(t.priority, "🟡")
    line = f"{mark} #{t.id} {t.title}"
    if t.due_at:
        line += f" — due {t.due_at.replace(tzinfo=_UTC).astimezone(tz or _DEFAULT_TZ):%a %d %b}"
    if t.notes:
        line += f"\n     {t.notes}"
    return line


def add_tasks(telegram_id: int, tasks: list) -> str:
    """Add one or many tasks. Use for a messy brain-dump: split it into separate items."""
    if isinstance(tasks, (str, dict)):
        tasks = [tasks]
    if not tasks:
        return "No tasks given."
    added = []
    tz = _tz(telegram_id)
    for item in tasks[:40]:
        if isinstance(item, str):
            item = {"title": item}
        title = str(item.get("title") or "").strip()
        if not title:
            continue
        tid = db.add_task(
            telegram_id, title, (item.get("notes") or None),
            int(item.get("priority") or 2), _due_utc(item.get("due_iso"), tz),
        )
        added.append(f"#{tid} {title}")
    if not added:
        return "None of those had a title I could use."
    total = db.count_open_tasks(telegram_id)
    return (f"📝 Added {len(added)} task(s):\n" + "\n".join(f"• {a}" for a in added)
            + f"\n\n{total} open in total.")


def list_open_tasks(telegram_id: int, when: str = "all") -> str:
    """List the user's open tasks. `when`: all | today | overdue."""
    tz = _tz(telegram_id)
    now_local = datetime.now(tz)
    cutoff = None
    if when == "today":
        end = now_local.replace(hour=23, minute=59, second=59)
        cutoff = end.astimezone(_UTC).replace(tzinfo=None)
    elif when == "overdue":
        cutoff = now_local.astimezone(_UTC).replace(tzinfo=None)
    rows = db.list_tasks(telegram_id, "open", due_before=cutoff)
    if not rows:
        if when == "today":
            return "Nothing due today. 🎉"
        if when == "overdue":
            return "Nothing overdue. 🎉"
        return "No open tasks. 🎉"
    head = {"today": "Due today", "overdue": "Overdue"}.get(when, "Open tasks")
    return f"{head} ({len(rows)}):\n" + "\n".join(_task_line(t) for t in rows)


def _pick_task(telegram_id: int, task_id, title: str | None):
    """Resolve a task from an id and/or a word from its title.

    Models often send a placeholder id (0) alongside the title, so a falsy id is
    treated as 'not given', and a bad id still falls back to the title.
    """
    try:
        tid = int(task_id) if task_id not in (None, "") else None
    except (TypeError, ValueError):
        tid = None
    if tid is not None and tid > 0 and db.get_task(telegram_id, tid) is not None:
        return tid, None
    if title:
        matches = db.find_tasks(telegram_id, title)
        if len(matches) == 1:
            return matches[0].id, None
        if len(matches) > 1:
            return None, "Which one?\n" + "\n".join(_task_line(t) for t in matches[:8])
        return None, f"No open task matching '{title}'."
    if tid is not None and tid > 0:
        return None, f"No task #{tid}."
    return None, "Tell me which task — its number or a word from it."


def complete_task(telegram_id: int, task_id: int | None = None,
                  title: str | None = None) -> str:
    """Tick a task off, by its id or by a word from its title."""
    task_id, problem = _pick_task(telegram_id, task_id, title)
    if problem:
        return problem
    t = db.set_task_status(telegram_id, int(task_id), "done")
    if t is None:
        return f"No task #{task_id}."
    return f"✅ Done: {t.title}\n{db.count_open_tasks(telegram_id)} still open."


def update_task(telegram_id: int, task_id: int, title: str | None = None,
                notes: str | None = None, priority: int | None = None,
                due_iso: str | None = None) -> str:
    """Change a task's title, notes, priority or due date."""
    t = db.update_task(telegram_id, int(task_id), title, notes, priority,
                       _due_utc(due_iso, _tz(telegram_id)))
    if t is None:
        return f"No task #{task_id}."
    return "✏️ Updated:\n" + _task_line(t)


def recall(telegram_id: int, about: str, limit: int = 12) -> str:
    """Search this user's own past — by MEANING first, keywords as backup.

    Semantic hits catch "what did I decide about caching" when the message
    actually said "Redis cache-aside". Keyword hits catch exact names and
    numbers that embeddings blur. Both are used, and duplicates dropped.
    """
    cap = max(1, min(int(limit or 12), 25))
    seen: set[str] = set()
    out = []

    for hit in memory.search(telegram_id, about, cap):
        key = hit["text"][:120]
        if key in seen:
            continue
        seen.add(key)
        when = hit["when"].strftime("%d %b") if hit.get("when") else "earlier"
        out.append(f"[{when}] {hit['text'][:300]}")

    for r in db.search_turns(telegram_id, about, cap):
        key = (r["content"] or "")[:120]
        if key in seen:
            continue
        seen.add(key)
        who = "You" if r["role"] == "user" else "Me"
        out.append(f"[{r['when']}] {who}: {r['content'][:300]}")

    if not out:
        return f"Nothing in our history about '{about}'."
    how = "" if memory.available() else " (keyword search only — semantic memory is off)"
    return (f"From our earlier chats about '{about}'{how}:\n"
            + "\n".join(out[:cap]))


def web_search(telegram_id: int, query: str, count: int = 5) -> str:
    """Search the open web — free, no API key needed."""
    try:
        hits = web.search(query, count)
    except Exception as e:  # noqa: BLE001 — report it, never crash the turn
        return (f"Couldn't search just now ({e}). Answer from what you know, "
                "and say plainly that you couldn't verify it.")
    if not hits:
        return f"No results for '{query}'."
    out = []
    for h in hits:
        line = f"• {h['title']}\n  {h['url']}"
        if h["snippet"]:
            line += f"\n  {h['snippet'][:220]}"
        out.append(line)
    return (f"🔎 Web results for '{query}':\n" + "\n".join(out)
            + "\n\n(These are snippets. Call read_page on the best link before "
              "stating a number, price, version or command.)")


def read_page(telegram_id: int, url: str, max_chars: int = 6000) -> str:
    """Fetch one page and return its readable text."""
    try:
        text = web.read(url, max_chars)
    except Exception as e:  # noqa: BLE001
        return f"Couldn't read that page: {e}"
    return f"📄 From {url}:\n\n{text}"


def list_recent_changes(telegram_id: int, limit: int = 8) -> str:
    """What was changed recently, newest first — the undo menu."""
    rows = db.recent_actions(telegram_id, max(1, min(int(limit or 8), 20)))
    if not rows:
        return "No changes recorded yet."
    lines = []
    for i, a in enumerate(rows, 1):
        local = a.created_at.replace(tzinfo=_UTC).astimezone(_tz(telegram_id))
        mark = " (already undone)" if a.undone else ""
        lines.append(f"{i}. {local:%d %b %H:%M} — {a.summary or a.tool}{mark}")
    return ("Recent changes (newest first):\n" + "\n".join(lines)
            + "\n\nSay 'undo that' for the newest, or 'undo 3' for the third.")


def undo_last(telegram_id: int, steps: int = 1) -> str:
    """Roll the plan and reminders back to before a recent change.

    Snapshot-based, so it correctly reverses ANY plan/reminder tool — including
    a clear_plan or an add_plan that replaced a track. Money is untouched:
    transactions have their own undo, and quietly rewinding accounting inside a
    plan undo would be unsafe.
    """
    try:
        n = max(1, int(steps or 1))
    except (TypeError, ValueError):
        n = 1
    rows = db.recent_actions(telegram_id, 20, only_undoable=True)
    if not rows:
        return ("Nothing to undo — I have no recorded change for you yet. "
                "(Money is separate: use undo_last_transaction for that.)")
    if n > len(rows):
        return (f"I only have {len(rows)} undoable change(s). "
                + list_recent_changes(telegram_id))
    target = rows[n - 1]
    try:
        snap = json.loads(target.snapshot)
    except (TypeError, ValueError):
        return "That undo point is unreadable, sorry — nothing was changed."
    tasks, rems = db.restore_user(telegram_id, snap)
    # Everything from this point forward is now void, so it can't be re-applied.
    for a in rows[:n]:
        db.mark_undone(telegram_id, a.id)
    local = target.created_at.replace(tzinfo=_UTC).astimezone(_tz(telegram_id))
    return (f"↩️ Rolled back to before: {target.summary or target.tool} "
            f"({local:%d %b %H:%M}).\n"
            f"Your plan is restored to {tasks} item(s) and {rems} reminder(s).\n"
            "Say 'show plan' to check it.")


def remember_about_me(telegram_id: int, fact: str, replace: bool = False) -> str:
    """Save something durable about who this user is, so it survives the conversation."""
    fact = (fact or "").strip()
    if not fact:
        return "Nothing to remember."
    current = db.get_profile(telegram_id) or ""
    if replace:
        new = fact
    else:
        lines = [l for l in current.splitlines() if l.strip()]
        if any(fact.lower() == l.strip("- ").lower() for l in lines):
            return "Already knew that."
        lines.append(f"- {fact}")
        new = "\n".join(lines[-40:])          # keep the profile from growing forever
    db.set_profile(telegram_id, new)
    return f"🧠 Noted about you: {fact}"


def set_my_place(telegram_id: int, timezone: str | None = None,
                 country: str | None = None, currency: str | None = None,
                 checkin_hour: int | None = None) -> str:
    """Save where THIS user lives, so their times and money are read correctly."""
    if timezone:
        try:
            ZoneInfo(timezone)
        except Exception:  # noqa: BLE001
            return (f"'{timezone}' isn't a timezone I know. Use an IANA name like "
                    "Asia/Kolkata, Europe/Berlin, America/New_York, Asia/Dubai.")
    if not db.set_locale(telegram_id, timezone, country, currency, checkin_hour):
        return "Couldn't save that."
    loc = db.get_locale(telegram_id)
    now = datetime.now(ZoneInfo(loc["timezone"]))
    return (f"📍 Saved: {loc['country']}, {loc['timezone']} "
            f"(it's {now:%H:%M} there now), money in {loc['currency']}, "
            f"evening check-in at {loc['checkin_hour']:02d}:00.")


def my_place(telegram_id: int) -> str:
    """Show the place settings currently in use for this user."""
    loc = db.get_locale(telegram_id)
    now = datetime.now(ZoneInfo(loc["timezone"]))
    known = "" if loc["known"] else "  (default — you haven't told me yet)"
    return (f"📍 {loc['country']}, {loc['timezone']}{known}\n"
            f"Local time now: {now:%A %d %b, %H:%M}\n"
            f"Money: {loc['currency']} · evening check-in {loc['checkin_hour']:02d}:00")


def my_profile(telegram_id: int) -> str:
    """Show what the assistant knows about this user."""
    p = db.get_profile(telegram_id)
    if not p:
        return ("I don't know anything about you yet. Tell me what you do, what you're "
                "working towards, and what matters to you — I'll remember it.")
    return "🧠 What I know about you:\n" + p + "\n\n(Say 'forget that ...' to correct me.)"


def forget_about_me(telegram_id: int, fact: str | None = None) -> str:
    """Drop one remembered line, or the whole profile."""
    current = db.get_profile(telegram_id) or ""
    if not current:
        return "Nothing stored about you."
    if not fact:
        db.set_profile(telegram_id, None)
        return "🧹 Cleared everything I knew about you."
    kept = [l for l in current.splitlines() if fact.lower() not in l.lower()]
    if len(kept) == len(current.splitlines()):
        return f"Nothing matching '{fact}' in your profile."
    db.set_profile(telegram_id, "\n".join(kept))
    return f"🧹 Forgot the bit about '{fact}'."


def _node_from(telegram_id: int, item: dict, parent_id: int | None,
               track: str | None, idx: int) -> int:
    """Create one plan node and everything under it. Returns how many were made."""
    title = str(item.get("title") or "").strip()
    if not title:
        return 0
    tz = _tz(telegram_id)
    kind = str(item.get("kind") or ("phase" if item.get("children") else "task")).lower()
    if kind not in ("track", "phase", "task", "habit"):
        kind = "task"
    node_id = db.add_node(
        telegram_id, title, kind, parent_id, track or item.get("track"),
        item.get("notes"), item.get("gate"), int(item.get("priority") or 2),
        item.get("target"), item.get("recur"), idx, _due_utc(item.get("due_iso"), tz),
    )
    made = 1
    for i, child in enumerate(item.get("children") or []):
        made += _node_from(telegram_id, child, node_id,
                           track or item.get("track"), i)
    return made


def disconnect_sheet(telegram_id: int, name: str | None = None) -> str:
    """Unlink a connected Google Sheet (the sheet itself is never touched)."""
    rows = db.list_sheets(telegram_id)
    if not rows:
        return "No sheets connected."
    targets = rows if not name else [
        r for r in rows if name.lower() in (r.title or "").lower()]
    if not targets:
        return ("No connected sheet matches that. You have: "
                + ", ".join(r.title or "Sheet" for r in rows))
    gone = [r.title or "Sheet" for r in targets if db.remove_sheet(telegram_id, r.sheet_id)]
    left = db.list_sheets(telegram_id)
    return (f"🔌 Disconnected: {', '.join(gone)}. "
            + (f"Still connected: {', '.join(r.title or 'Sheet' for r in left)}."
               if left else "No sheets connected now.")
            + "\n(The Google Sheet itself is untouched — I only removed the link.)")


def clear_reminders(telegram_id: int) -> str:
    """Delete every reminder, including repeating ones."""
    n = db.delete_all_reminders(telegram_id)
    return f"🗑 Cleared {n} reminder(s)." if n else "No reminders to clear."


def reset_everything(telegram_id: int, confirm: bool = False) -> str:
    """Wipe this user's plan, tasks, reminders, profile and sheet links.

    Destructive and irreversible, so it refuses unless `confirm` is true — the
    assistant must ask the user first and only pass true once they've said yes.
    """
    if not confirm:
        counts = (f"{db.count_open_tasks(telegram_id)} open task(s), "
                  f"{len(db.list_reminders(telegram_id))} reminder(s), "
                  f"{db.count_sheets(telegram_id)} sheet link(s)")
        return ("⚠️ NOT done yet — this wipes your whole plan, tasks, reminders, "
                f"profile and sheet links ({counts}). It cannot be undone. "
                "Ask the user to confirm, then call this again with confirm=true.")
    tasks = db.delete_all_tasks(telegram_id)
    rem = db.delete_all_reminders(telegram_id)
    sheets_n = 0
    for r in db.list_sheets(telegram_id):
        if db.remove_sheet(telegram_id, r.sheet_id):
            sheets_n += 1
    db.set_profile(telegram_id, None)
    return (f"🧹 Fresh start: removed {tasks} plan/task item(s), {rem} reminder(s), "
            f"{sheets_n} sheet link(s), and cleared what I knew about you.\n"
            "Your Google Sheets and Drive files themselves are untouched.")


def clear_plan(telegram_id: int, track: str | None = None) -> str:
    """Delete a whole track (with everything under it), or the entire plan."""
    if track:
        hits = db.find_tracks(telegram_id, track)
        if not hits:
            names = ", ".join(t.title for t in db.tracks(telegram_id)) or "nothing stored"
            return f"No track matching '{track}'. You have: {names}."
        gone = sum(db.delete_subtree(telegram_id, t.id) for t in hits)
        killed = ", ".join(t.title for t in hits)
        return f"🗑 Removed {killed} ({gone} items). Left: " + (
            ", ".join(t.title for t in db.tracks(telegram_id)) or "nothing")
    n = db.delete_all_tasks(telegram_id)
    return f"🗑 Cleared your whole plan ({n} items). Send me the new one."


def add_plan(telegram_id: int, plan: list, replace: bool = False) -> str:
    """Store a whole structured plan as a tree: tracks → phases → tasks/habits.

    `replace` wipes the existing plan first — otherwise re-sending a corrected
    plan just stacks another copy of every track next to the old one.
    """
    if isinstance(plan, dict):
        plan = [plan]
    if not plan:
        return "Empty plan."
    wiped = ""
    if replace:
        n = db.delete_all_tasks(telegram_id)
        wiped = f"Replaced the old plan ({n} items removed).\n"
    else:
        # Same-named track already there? Replace that one instead of duplicating.
        for top in plan:
            if isinstance(top, dict) and top.get("title"):
                for old in db.find_tracks(telegram_id, str(top["title"])):
                    if (old.title or "").strip().lower() == str(top["title"]).strip().lower():
                        db.delete_subtree(telegram_id, old.id)
                        wiped = "Updated the existing track(s) instead of duplicating.\n"
    total = 0
    names = []
    for i, top in enumerate(plan[:12]):
        if not isinstance(top, dict):
            continue
        top.setdefault("kind", "track")
        track = top.get("track") or top.get("title")
        n = _node_from(telegram_id, top, None, track, i)
        if n:
            total += n
            names.append(f"{top.get('title')} ({n - 1} under it)")
    if not total:
        return "Nothing in that plan had a title I could use."
    return (wiped + "🌳 Plan saved:\n" + "\n".join(f"• {n}" for n in names)
            + f"\n\n{total} nodes stored. Ask 'show plan' any time, or 'what now?'.")


def _node_label(t) -> str:
    """One readable line for a plan node — used when confirming an edit."""
    bits = [f"#{t.id} {t.title}"]
    if t.kind != "task":
        bits.append(f"({t.kind})")
    if t.track:
        bits.append(f"in {t.track}")
    line = " ".join(bits)
    if t.target:
        line += f" — {t.progress or 0}/{t.target}"
    if t.status != "open":
        line += f" — {t.status}"
    if t.recur:
        line += f" — {t.recur}"
    if t.gate:
        line += f"\n     gate: {t.gate}"
    if t.notes:
        line += f"\n     notes: {t.notes[:200]}"
    return line


def _fuzzy_nodes(telegram_id: int, title: str, kinds: tuple[str, ...] | None = None,
                 cutoff: int = 72) -> list:
    """Approximate title match — catches typos and rewordings.

    Only ever used AFTER an exact/substring search found nothing, so it can't
    hijack a match the user clearly meant. Degrades to [] without rapidfuzz.
    """
    if fuzz is None or not (title or "").strip():
        return []
    rows = [t for t in db.list_tasks(telegram_id, status="all", limit=500)
            if not kinds or t.kind in kinds]
    scored = []
    for t in rows:
        name = (t.title or "")
        # partial_ratio so "histogram" scores against "P3 Stack — histogram cold".
        score = max(fuzz.WRatio(title, name), fuzz.partial_ratio(title.lower(),
                                                                name.lower()))
        if score >= cutoff:
            scored.append((score, t))
    if not scored:
        return []
    scored.sort(key=lambda x: -x[0])
    best = scored[0][0]
    # Keep only the clearly-best group, so we ask "which?" only on a real tie.
    return [t for s, t in scored if best - s <= 5][:8]


def _pick_node(telegram_id: int, item_id, title: str | None,
               kinds: tuple[str, ...] | None = None):
    """Resolve ANY plan node from an id and/or words from its title.

    Unlike _pick_task this finds tracks, phases, habits and finished items too,
    so 'change the P3 gate' or 'reopen P1' can actually land on something.
    """
    try:
        nid = int(item_id) if item_id not in (None, "") else None
    except (TypeError, ValueError):
        nid = None
    if nid is not None and nid > 0:
        t = db.get_task(telegram_id, nid)
        if t is not None:
            return t, None
    if title:
        matches = db.find_nodes(telegram_id, title, kinds)
        if not matches:
            # Nothing matched literally — try approximately, so "P3 stak" and
            # "the histogram phase" still land instead of dead-ending.
            matches = _fuzzy_nodes(telegram_id, title, kinds)
        if len(matches) == 1:
            return matches[0], None
        if len(matches) > 1:
            return None, ("More than one matches — which?\n"
                          + "\n".join(_node_label(m) for m in matches[:8]))
        return None, f"Nothing in your plan matches '{title}'."
    if nid is not None and nid > 0:
        return None, f"No item #{nid}."
    return None, "Tell me which item — its number or a few words from it."


def edit_plan_item(telegram_id: int, item_id: int | None = None,
                   title: str | None = None, new_title: str | None = None,
                   notes: str | None = None, gate: str | None = None,
                   target: int | None = None, progress: int | None = None,
                   recur: str | None = None, priority: int | None = None,
                   status: str | None = None, due_iso: str | None = None) -> str:
    """Change one field of an existing plan item in place — no re-sending the plan.

    This is the fix for "the P3 gate is wrong": before, the only way to correct a
    phase was add_plan with replace, which threw away all recorded progress.
    """
    node, problem = _pick_node(telegram_id, item_id, title)
    if problem:
        return problem
    t = db.update_task(
        telegram_id, node.id, new_title, notes, priority,
        _due_utc(due_iso, _tz(telegram_id)), False, gate, target, progress, recur, status,
    )
    if t is None:
        return f"Couldn't update #{node.id}."
    return "✏️ Updated:\n" + _node_label(t)


def add_to_plan(telegram_id: int, parent: str, items: list) -> str:
    """Append phases/tasks/habits UNDER an existing track or phase.

    Without this, adding one phase meant calling add_plan again — which replaces
    the same-named track and silently destroys every other phase under it.
    """
    node, problem = _pick_node(telegram_id, None, parent,
                               ("track", "phase", "task"))
    if problem:
        return problem
    if isinstance(items, (str, dict)):
        items = [items]
    if not items:
        return "Nothing to add."
    start = db.next_order_idx(telegram_id, node.id)
    made = 0
    names = []
    for i, item in enumerate(items[:40]):
        if isinstance(item, str):
            item = {"title": item}
        if not isinstance(item, dict) or not str(item.get("title") or "").strip():
            continue
        n = _node_from(telegram_id, item, node.id, node.track or node.title,
                       start + i)
        if n:
            made += n
            names.append(str(item["title"]))
    if not made:
        return "None of those had a title I could use."
    return (f"➕ Added under {node.title}:\n"
            + "\n".join(f"• {n}" for n in names)
            + f"\n\n{made} node(s) stored. Everything already in {node.title} is untouched.")


def remove_plan_item(telegram_id: int, item_id: int | None = None,
                     title: str | None = None) -> str:
    """Delete ONE phase/task/habit (and anything under it) without touching the rest."""
    node, problem = _pick_node(telegram_id, item_id, title)
    if problem:
        return problem
    label = node.title
    gone = db.delete_subtree(telegram_id, node.id)
    extra = f" and {gone - 1} item(s) under it" if gone > 1 else ""
    return f"🗑 Removed {label}{extra}. The rest of your plan is untouched."


def reopen_item(telegram_id: int, item_id: int | None = None,
                title: str | None = None) -> str:
    """Put a wrongly-completed or dropped item back to open."""
    node, problem = _pick_node(telegram_id, item_id, title)
    if problem:
        return problem
    if node.status == "open":
        return f"{node.title} is already open."
    t = db.update_task(telegram_id, node.id, status="open")
    return f"↩️ Reopened: {t.title}" if t else f"Couldn't reopen #{node.id}."


def _bar(done: int, total: int) -> str:
    if not total:
        return ""
    filled = round(done / total * 10)
    return f" [{'█' * filled}{'░' * (10 - filled)}] {done}/{total}"


def _render(telegram_id: int, node, depth: int, out: list, max_depth: int) -> None:
    pad = "  " * depth
    done, total = db.subtree_stats(telegram_id, node.id)
    mark = {"done": "✅", "dropped": "🗑"}.get(node.status, "▫️")
    if node.kind == "habit":
        mark = "🔁"
    line = f"{pad}{mark} {node.title}"
    if total:
        line += _bar(done, total)
    elif node.target:
        line += f" ({node.progress or 0}/{node.target})"
    if node.kind == "habit" and node.streak:
        line += f" 🔥{node.streak}"
    out.append(line)
    if node.gate and depth <= 1:
        out.append(f"{pad}   gate: {node.gate}")
    if depth < max_depth:
        for kid in db.children(telegram_id, node.id):
            _render(telegram_id, kid, depth + 1, out, max_depth)


def _summary_line(telegram_id: int, t) -> str:
    """One line per track: progress bar + the phase actually in play."""
    done, total = db.subtree_stats(telegram_id, t.id)
    bar = _bar(done, total).strip() if total else ""
    line = f"{t.title} {bar}".rstrip()
    nxt = next((k for k in db.children(telegram_id, t.id)
                if k.status == "open" and k.kind != "habit"), None)
    if nxt:
        bit = f"\n   → now: {nxt.title}"
        if nxt.target:
            bit += f" ({nxt.progress or 0}/{nxt.target})"
        line += bit
    return line


def show_plan(telegram_id: int, track: str | None = None, full: bool = False) -> str:
    """The plan. Short by default — a wall of every phase and gate is unreadable
    on a phone. `track` opens one area, `full` dumps everything."""
    tops = db.tracks(telegram_id)
    if not tops:
        return ("No plan stored yet. Send me your plan (paste it or attach a file) "
                "and I'll build the tree — tracks, phases and gates.")

    # One track asked for: its phases, gate only on the one in play.
    if track:
        hits = [t for t in tops if track.lower() in (t.title or "").lower()]
        if not hits:
            return "No track by that name. You have: " + ", ".join(t.title for t in tops)
        out = []
        for t in hits[:2]:
            done, total = db.subtree_stats(telegram_id, t.id)
            out.append(f"🌳 {t.title} {_bar(done, total).strip()}")
            shown_gate = False
            for k in db.children(telegram_id, t.id):
                mark = {"done": "✅", "dropped": "🗑"}.get(k.status, "▫️")
                if k.kind == "habit":
                    mark = "🔁"
                row = f"  {mark} {k.title}"
                if k.target:
                    row += f" ({k.progress or 0}/{k.target})"
                if k.streak:
                    row += f" 🔥{k.streak}"
                out.append(row)
                if k.status == "open" and k.gate and not shown_gate:
                    out.append(f"       gate: {k.gate}")
                    shown_gate = True
            out.append("")
        return "\n".join(out).strip()[:3800]

    if full:                                   # everything, on explicit request
        out: list[str] = []
        for t in tops:
            _render(telegram_id, t, 0, out, 2)
            out.append("")
        return "\n".join(out).strip()[:3800]

    # Default: one block per track, plus habits. Short enough to actually read.
    out = ["🌳 Your plan"]
    for t in tops:
        out.append(_summary_line(telegram_id, t))
    habits = db.habits(telegram_id)
    if habits:
        out.append("\n🔁 " + ", ".join(
            h.title + (f" 🔥{h.streak}" if h.streak else "") for h in habits[:8]))
    names = ", ".join(t.title for t in tops[:4])
    out.append(f"\nSay \"show {tops[0].title}\" for its phases, or \"full plan\" for everything.")
    return "\n".join(out)[:3800]


def plan_snapshot(telegram_id: int, max_chars: int = 2200) -> str:
    """A compact always-on view of the plan for the system prompt.

    Only what's needed to stay aware: each track, the phase currently in play,
    its gate and progress, and habits. Kept small — show_plan gives the full tree.
    """
    tops = db.tracks(telegram_id)
    if not tops:
        return ""
    out = []
    for t in tops:
        done, total = db.subtree_stats(telegram_id, t.id)
        head = f"{t.title}"
        if total:
            head += f" ({done}/{total} done)"
        kids = [k for k in db.children(telegram_id, t.id) if k.status == "open"]
        habit_kids = [k for k in kids if k.kind == "habit"]
        work_kids = [k for k in kids if k.kind != "habit"]
        if work_kids:
            cur = work_kids[0]
            bit = f" — now: {cur.title}"
            if cur.target:
                bit += f" [{cur.progress or 0}/{cur.target}]"
            if cur.gate:
                bit += f" (gate: {cur.gate})"
            head += bit
        if habit_kids:
            head += " — habits: " + ", ".join(
                h.title + (f" 🔥{h.streak}" if h.streak else "") for h in habit_kids)
        out.append("• " + head)
    text = "\n".join(out)
    return text[:max_chars] + ("…" if len(text) > max_chars else "")


def what_now(telegram_id: int) -> str:
    """The single next thing to do, decided from the plan — not from memory."""
    tz = _tz(telegram_id)
    now_local = datetime.now(tz)
    now_utc = now_local.astimezone(_UTC).replace(tzinfo=None)
    lines = []

    overdue = [t for t in db.list_tasks(telegram_id, "open", due_before=now_utc)]
    if overdue:
        lines.append("⚠️ Overdue first:\n" + "\n".join(_task_line(t) for t in overdue[:3]))

    pending_habits = []
    for h in db.habits(telegram_id):
        last = h.last_done_at
        if last is None or last.date() < now_utc.date():
            pending_habits.append(h)

    # The next open item in EACH track, so no area silently stalls.
    seen: set[str] = set()
    blocks = []
    for t in db.open_leaves(telegram_id):
        key = t.track or "—"
        if key in seen:
            continue
        seen.add(key)
        parent = db.get_task(telegram_id, t.parent_id) if t.parent_id else None
        head = f"{key}: {t.title}" if t.track else t.title
        block = f"👉 {head}"
        if t.target:
            block += f"\n   at {t.progress or 0}/{t.target}"
        gate = t.gate or (parent.gate if parent else None)
        if gate:
            block += f"\n   clears when: {gate}"
        blocks.append(block)
    if blocks:
        lines.append("Next up:\n" + "\n".join(blocks))

    if pending_habits:
        lines.append("🔁 Not done today: "
                     + ", ".join(f"{h.title}" + (f" 🔥{h.streak}" if h.streak else "")
                                 for h in pending_habits))
    if not lines:
        return "Nothing open. Either you're done, or the plan needs its next phase. 🎉"
    return "\n\n".join(lines)


def log_progress(telegram_id: int, task_id: int | None = None,
                 title: str | None = None, count: int = 1) -> str:
    """Record countable progress — 'solved 5 problems', 'did 2 designs'."""
    task_id, problem = _pick_task(telegram_id, task_id, title)
    if problem:
        return problem
    t = db.bump_progress(telegram_id, int(task_id), int(count or 1))
    if t is None:
        return f"No task #{task_id}."
    if t.target:
        left = max(0, t.target - (t.progress or 0))
        done_note = " — phase cleared! 🎉" if t.status == "done" else f" — {left} to go"
        return f"📈 {t.title}: {t.progress}/{t.target}{done_note}"
    return f"📈 {t.title}: {t.progress} logged."


def check_habit(telegram_id: int, title: str) -> str:
    """Tick off a repeating habit (calisthenics, X post, reading) and keep the streak."""
    matches = [h for h in db.habits(telegram_id) if title.lower() in (h.title or "").lower()]
    if not matches and fuzz is not None:
        # "did callisthenics" / "gym done" should still tick the right habit.
        scored = [(fuzz.WRatio(title, h.title or ""), h) for h in db.habits(telegram_id)]
        matches = [h for s, h in sorted(scored, key=lambda x: -x[0]) if s >= 72][:1]
    if not matches:
        names = ", ".join(h.title for h in db.habits(telegram_id)) or "none set up"
        return f"No habit matching '{title}'. Your habits: {names}."
    now_utc = datetime.now(_tz(telegram_id)).astimezone(_UTC).replace(tzinfo=None)
    h = db.touch_habit(telegram_id, matches[0].id, now_utc)
    return f"🔁 {h.title} done. Streak: {h.streak} 🔥"


def drop_task(telegram_id: int, task_id: int) -> str:
    """Remove a task that's no longer needed (not done — just cancelled)."""
    t = db.set_task_status(telegram_id, int(task_id), "dropped")
    if t is None:
        return f"No task #{task_id}."
    return f"🗑 Dropped: {t.title}"


# =========================================================================
#  Password vault (encrypted at rest)
# =========================================================================
def save_password(telegram_id: int, name: str, secret: str, username: str | None = None) -> str:
    db.save_secret(telegram_id, name, crypto.encrypt(secret), username)
    return f"🔒 Saved credential '{name}' (encrypted)."


# A retrieved secret waits here for the bot to hand it over itself, instead of
# being returned to the model. Three things went wrong when the plaintext came
# back through the reply: it was written into the conversation table next to the
# encrypted copy (so the vault's encryption bought nothing against anyone
# holding the .db file), it was re-sent to the LLM provider with every following
# turn while it sat in the history window, and it stayed in Telegram forever.
# A plain dict, not a ContextVar: the tool runs in a worker thread and the
# handler reads this from the event loop, and context does not cross that.
_pending_secret: dict[int, str] = {}


def take_pending_secret(telegram_id: int) -> str | None:
    """Pop the secret this user just asked for, for the bot to send directly."""
    return _pending_secret.pop(telegram_id, None)


def get_password(telegram_id: int, name: str) -> str:
    row = db.get_secret(telegram_id, name)
    if not row:
        return f"No saved credential named '{name}'. Use 'list passwords' to see names."
    secret = crypto.decrypt(row.secret_enc)
    user_line = f"User: {row.username}\n" if row.username else ""
    _pending_secret[telegram_id] = (
        f"🔐 {row.name}\n{user_line}Password: {secret}\n\n"
        f"This message deletes itself in 90 seconds.")
    # What the MODEL sees — deliberately not the secret.
    return (f"Found '{row.name}'. It has been sent to the user in a separate "
            f"message, which self-deletes. You do NOT have the value and must "
            f"not ask for it. Just tell them it's above.")


def list_passwords(telegram_id: int) -> str:
    names = db.list_secret_names(telegram_id)
    return "Saved credentials: " + (", ".join(names) if names else "none yet.")


def delete_password(telegram_id: int, name: str) -> str:
    return "Deleted." if db.delete_secret(telegram_id, name) else f"No credential named '{name}'."


# =========================================================================
#  Google sheet / drive: connect & read (share-a-sheet model)
# =========================================================================
def _pick_transaction(telegram_id: int, tx_id, match: str | None):
    """Find a money entry by id OR by words from its category/note/amount.

    Models often pass a placeholder id of 0 next to the description, so a falsy
    id counts as absent and a wrong id still falls back to the description.
    """
    try:
        tid = int(tx_id) if tx_id not in (None, "") else None
    except (TypeError, ValueError):
        tid = None
    rows = db.list_transactions(telegram_id, 40)
    if tid is not None and tid > 0 and any(r.id == tid for r in rows):
        return tid, None
    if match:
        m = str(match).strip().lower()
        hits = [r for r in rows
                if m in (r.category or "").lower()
                or m in (r.note or "").lower()
                or m in f"{r.amount:.0f}"]
        if len(hits) == 1:
            return hits[0].id, None
        if len(hits) > 1:
            listing = "\n".join(
                f"#{r.id} - {r.amount:.2f} {'in' if r.kind == 'in' else 'out'}"
                + (f" - {r.category}" if r.category else "") for r in hits[:8])
            return None, "Which one?\n" + listing
        return None, f"No entry matching '{match}'."
    if tid is not None and tid > 0:
        return None, f"No transaction #{tid}."
    return None, "Which entry? Give its number or a word from it."


def _pick_reminder(telegram_id: int, rem_id, match: str | None):
    """Find a reminder by id OR by words from its text."""
    try:
        rid = int(rem_id) if rem_id not in (None, "") else None
    except (TypeError, ValueError):
        rid = None
    rows = db.list_reminders(telegram_id)
    if rid is not None and rid > 0 and any(r.id == rid for r in rows):
        return rid, None
    if match:
        m = str(match).strip().lower()
        hits = [r for r in rows if m in (r.text or "").lower()]
        if len(hits) == 1:
            return hits[0].id, None
        if len(hits) > 1:
            return None, "Which one?\n" + "\n".join(
                f"#{r.id} - {r.text}" for r in hits[:8])
        return None, f"No reminder matching '{match}'."
    if rid is not None and rid > 0:
        return None, f"No reminder #{rid}."
    return None, "Which reminder? Give its number or a word from it."


def list_transactions(telegram_id: int, limit: int = 10) -> str:
    """Recent money entries with their ids, so any of them can be fixed."""
    rows = db.list_transactions(telegram_id, max(1, min(int(limit or 10), 30)))
    if not rows:
        return "No transactions logged yet."
    out = []
    for r in rows:
        arrow = "in" if r.kind == "in" else "out"
        when = r.occurred_at.strftime("%d %b") if r.occurred_at else ""
        out.append(f"#{r.id} - {when} - {r.amount:.2f} {arrow}"
                   + (f" - {r.category}" if r.category else "")
                   + (f" ({r.note})" if r.note else ""))
    return "Recent entries:\n" + "\n".join(out)


def edit_transaction(telegram_id: int, transaction_id: int | None = None,
                     amount: float | None = None, kind: str | None = None,
                     category: str | None = None, note: str | None = None,
                     match: str | None = None) -> str:
    """Correct ANY logged entry, found by id or by how the user described it."""
    transaction_id, problem = _pick_transaction(telegram_id, transaction_id, match)
    if problem:
        return problem
    if kind is not None:
        kind = "in" if str(kind).lower() in ("in", "credit", "income", "received") else "out"
    if amount is not None:
        amount = abs(float(amount))
    ok = db.update_transaction(telegram_id, int(transaction_id), amount, kind, category, note)
    if not ok:
        return f"No transaction #{transaction_id}."
    return f"✏️ Updated entry #{transaction_id}."


def delete_transaction(telegram_id: int, transaction_id: int | None = None,
                       match: str | None = None) -> str:
    """Delete a logged entry, found by id or description (local record only)."""
    transaction_id, problem = _pick_transaction(telegram_id, transaction_id, match)
    if problem:
        return problem
    removed = db.delete_transaction(telegram_id, int(transaction_id))
    if removed is None:
        return f"No transaction #{transaction_id}."
    direction = "in" if removed.kind == "in" else "out"
    return (f"🗑 Deleted #{transaction_id}: {removed.amount:.2f} {direction}.\n"
            "(The row in your Google Sheet isn't removed - delete it there if needed.)")


def edit_reminder(telegram_id: int, reminder_id: int | None = None,
                  text: str | None = None, when_iso: str | None = None,
                  repeat: str | None = None, match: str | None = None) -> str:
    """Change a reminder, found by id or by words from it."""
    reminder_id, problem = _pick_reminder(telegram_id, reminder_id, match)
    if problem:
        return problem
    due = None
    if when_iso:
        local = datetime.fromisoformat(when_iso)
        if local.tzinfo is None:
            local = local.replace(tzinfo=_tz(telegram_id))
        due = local.astimezone(_UTC).replace(tzinfo=None)
    clear = str(repeat).lower() in ("none", "off", "once", "never") if repeat else False
    r = db.update_reminder(telegram_id, int(reminder_id), text, due,
                           None if clear else repeat, clear)
    if r is None:
        return f"No reminder #{reminder_id}."
    local = r.due_at.replace(tzinfo=_UTC).astimezone(_tz(telegram_id))
    every = f" 🔁 {r.repeat}" if r.repeat else ""
    return f"✏️ Reminder #{r.id} -> {local:%a %d %b, %H:%M}{every}: {r.text}"


def add_habit(telegram_id: int, title: str, recur: str = "daily",
              notes: str | None = None) -> str:
    """Start tracking a repeating habit with a streak (gym, posting, reading)."""
    existing = [h for h in db.habits(telegram_id)
                if title.lower() in (h.title or "").lower()]
    if existing:
        return f"Already tracking '{existing[0].title}'."
    life = next((t for t in db.tracks(telegram_id)
                 if (t.title or "").lower() in ("life", "habits")), None)
    db.add_node(telegram_id, title, "habit", life.id if life else None,
                life.track if life else "Life", notes, None, 2, None, recur, 0)
    return f"🔁 Tracking '{title}' ({recur}). Say 'did {title}' to tick it off."


def list_habits(telegram_id: int) -> str:
    """Show habits with their streaks and whether today is done."""
    hs = db.habits(telegram_id)
    if not hs:
        return "No habits tracked yet. Say e.g. 'track calisthenics 4x a week'."
    today = datetime.now(_tz(telegram_id)).astimezone(_UTC).replace(tzinfo=None).date()
    out = []
    for h in hs:
        done = h.last_done_at is not None and h.last_done_at.date() == today
        out.append(f"{'✅' if done else '⬜'} {h.title}"
                   + (f" - {h.recur}" if h.recur else "")
                   + (f" 🔥{h.streak}" if h.streak else ""))
    return "Habits:\n" + "\n".join(out)


def remove_habit(telegram_id: int, title: str) -> str:
    """Stop tracking a habit."""
    hits = [h for h in db.habits(telegram_id) if title.lower() in (h.title or "").lower()]
    if not hits:
        return f"No habit matching '{title}'."
    db.delete_subtree(telegram_id, hits[0].id)
    return f"🗑 Stopped tracking '{hits[0].title}'."


def edit_bill_account(telegram_id: int, name: str, new_name: str | None = None,
                      due_day: int | None = None, statement_day: int | None = None) -> str:
    """Change a tracked bill's name or its due/statement day."""
    acc = db.update_bill(telegram_id, name, new_name, statement_day, due_day)
    if acc is None:
        return f"No tracked bill matching '{name}'."
    return (f"✏️ {acc.name} - due day {acc.due_day or '?'}, "
            f"statement day {acc.statement_day or '?'}.")


def delete_bill_account(telegram_id: int, name: str) -> str:
    """Stop tracking a bill."""
    gone = db.delete_bill(telegram_id, name)
    if not gone:
        return f"No tracked bill matching '{name}'."
    return f"🗑 Stopped tracking '{gone}'."


def remove_mailbox(telegram_id: int, email: str) -> str:
    """Disconnect an IMAP/SMTP mailbox."""
    if db.remove_mail_account(telegram_id, email):
        return f"🔌 Removed mailbox {email}."
    return f"No mailbox matching '{email}'."


def remove_google_account(telegram_id: int, email: str) -> str:
    """Unlink a Google account (Gmail/Drive/Calendar access stops)."""
    if db.remove_google_account(telegram_id, email):
        return f"🔌 Unlinked {email}."
    return f"No linked account matching '{email}'."


def set_default_google_account(telegram_id: int, email: str) -> str:
    """Choose which linked Google account is used by default."""
    accts = db.list_google_accounts(telegram_id)
    match = [a for a in accts if email.lower() in a.email.lower()]
    if not match:
        return "No linked account matches. Linked: " + (
            ", ".join(a.email for a in accts) or "none")
    db.set_default_account(telegram_id, match[0].email)
    return f"⭐ Default account is now {match[0].email}."


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
#  Obsidian vault — notes that live as real markdown the user owns
# =========================================================================
def _vault_hint(detail: str = "") -> str:
    base = ("No Obsidian vault linked yet, so I kept the note here only. "
            "Run /vault in Telegram to connect one — a GitHub repo (Obsidian "
            "Git plugin pulls it) or a folder on the machine running me.")
    return f"{base}\n({detail})" if detail else base


def _wrote(res: dict, verb: str = "Saved") -> str:
    line = f"📝 {verb} [[{res['title']}]] → {res['path']}"
    if res["pushed"]:
        return f"{line}\n✅ In your vault ({res['where']})."
    return f"{line}\n⚠️ {_vault_hint(res['detail'])}"


def write_note(telegram_id: int, title: str, content: str,
               folder: str | None = None, tags: list | None = None,
               mode: str = "replace") -> str:
    """Write a markdown note into the user's Obsidian vault."""
    if not (title or "").strip():
        return "A note needs a title — that's the name Obsidian links to."
    if mode not in ("replace", "append", "prepend"):
        mode = "replace"
    try:
        res = notes.write(telegram_id, title, content or "", folder,
                          [str(t) for t in (tags or [])], mode)
    except vaultmod.VaultError as e:
        return f"Couldn't write that note: {e}"
    verb = {"replace": "Saved", "append": "Added to", "prepend": "Added to"}[mode]
    return _wrote(res, verb)


def append_note(telegram_id: int, title: str, content: str,
                folder: str | None = None) -> str:
    """Add to the end of an existing note (creates it if it's new)."""
    return write_note(telegram_id, title, content, folder, None, "append")


def read_note(telegram_id: int, note: str) -> str:
    """Read one note back — by title, by path, or by roughly what it's called."""
    got = notes.read_note(telegram_id, note)
    if not got:
        near = db.find_notes(telegram_id, note, limit=5)
        if near:
            return ("No note called that. Closest: "
                    + ", ".join(f"'{n.title}'" for n in near))
        return f"No note matching '{note}'. {notes_count_hint(telegram_id)}"
    path, body = got
    links = notes.extract_links(body)
    out = f"📄 {notes.title_of(path)} ({path})\n\n{body[:4000]}"
    if len(body) > 4000:
        out += "\n… (truncated)"
    if links:
        out += "\n\nLinks out to: " + ", ".join(f"[[{l}]]" for l in links[:12])
    back = notes.backlinks(telegram_id, notes.title_of(path))
    if back:
        out += "\n Linked from: " + ", ".join(f"[[{b['title']}]]" for b in back[:12])
    return out


def notes_count_hint(telegram_id: int) -> str:
    n = db.count_notes(telegram_id)
    return f"You have {n} note(s)." if n else "You have no notes yet."


def search_notes(telegram_id: int, query: str, limit: int = 6) -> str:
    """Search the vault by meaning, not just words."""
    hits = notes.search(telegram_id, query, max(1, min(int(limit or 6), 15)))
    if not hits:
        return f"Nothing in your notes about '{query}'. {notes_count_hint(telegram_id)}"
    out = [f"🔎 Notes about '{query}':"]
    for h in hits:
        snippet = " ".join(h["body"].split())[:220]
        out.append(f"\n• [[{h['title']}]] ({h['path']})\n  {snippet}")
    return "\n".join(out)


def list_notes(telegram_id: int, folder: str | None = None, limit: int = 20) -> str:
    """List notes, newest first, optionally inside one folder."""
    rows = db.list_notes(telegram_id, folder, max(1, min(int(limit or 20), 60)))
    if not rows:
        return (f"No notes in '{folder}' yet." if folder
                else "No notes yet. Say 'note this: …' and I'll start the vault.")
    head = f"🗂 Notes in {folder}:" if folder else "🗂 Your notes:"
    return head + "\n" + "\n".join(
        f"• {r.title} — {r.path}"
        + (f"  #{r.tags.replace(',', ' #')}" if r.tags else "") for r in rows)


def note_backlinks(telegram_id: int, title: str) -> str:
    """Which notes link to this one — the Obsidian backlink pane, as text."""
    back = notes.backlinks(telegram_id, title)
    if not back:
        return f"Nothing links to [[{notes.title_of(title)}]] yet."
    return (f"🔗 Linked to [[{notes.title_of(title)}]]:\n"
            + "\n".join(f"• {b['title']} ({b['path']})" for b in back))


def daily_note(telegram_id: int, content: str | None = None,
               heading: str | None = None, day: str | None = None) -> str:
    """Read today's daily note, or add a line to it."""
    when = None
    if day:
        try:
            when = datetime.strptime(day.strip()[:10], "%Y-%m-%d").date()
        except ValueError:
            return "Give the day as YYYY-MM-DD, or leave it out for today."
    if not (content or "").strip():
        got = notes.read_note(telegram_id, notes.daily_path(when))
        if not got:
            return "Nothing in today's note yet."
        return f"📅 {got[0]}\n\n{got[1][:3000]}"
    try:
        res = notes.append_daily(telegram_id, content.strip(), heading, when)
    except vaultmod.VaultError as e:
        return f"Couldn't write the daily note: {e}"
    return _wrote(res, "Added to")


def capture_research(telegram_id: int, text: str, doing: str | None = None) -> str:
    """Park an off-topic idea in the research inbox so focus isn't broken."""
    if not (text or "").strip():
        return "Nothing to capture."
    try:
        res = notes.capture(telegram_id, text.strip(), doing)
    except vaultmod.VaultError as e:
        return f"Couldn't reach the inbox note: {e}"
    open_n = len(notes.inbox_items(telegram_id))
    line = f"📥 Parked for Sunday: {text.strip()[:80]}"
    if doing:
        line += f"\nYou're on {doing} — back to it."
    line += f"\n({open_n} thing(s) waiting in the inbox.)"
    if not res["pushed"]:
        line += f"\n⚠️ {_vault_hint(res['detail'])}"
    return line


def review_inbox(telegram_id: int, action: str = "list",
                 item: str | None = None) -> str:
    """Read the research inbox, or tick items off after reading them."""
    if action == "clear":
        n, detail = notes.inbox_close(telegram_id, item)
        if not n:
            return detail
        return f"✅ Marked {n} inbox item(s) read: {detail}"
    items = notes.inbox_items(telegram_id)
    if not items:
        return "📥 Research inbox is empty — nothing parked."
    return (f"📥 {len(items)} thing(s) parked:\n"
            + "\n".join(f"• {text}" for _, text, _ in items[:25])
            + "\n\nSay 'done with <words>' to tick one off, or 'clear the inbox'.")


def vault_status(telegram_id: int) -> str:
    """Where notes are going, and whether that link actually works."""
    line = vaultmod.status(telegram_id)
    ok, detail = vaultmod.check(telegram_id)
    count = db.count_notes(telegram_id)
    out = f"{line}\n{'✅' if ok else '⚠️'} {detail}\n🗂 {count} note(s) indexed here."
    if not ok:
        out += "\nRun /vault to link or fix it."
    return out


def sync_vault(telegram_id: int) -> str:
    """Pull notes edited in Obsidian, push anything that never made it out."""
    try:
        return notes.sync(telegram_id)
    except vaultmod.NotConnected as e:
        return str(e)
    except vaultmod.VaultError as e:
        return f"Vault sync failed: {e}"


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
    "add_tasks": add_tasks,
    "add_plan": add_plan,
    "clear_plan": clear_plan,
    "disconnect_sheet": disconnect_sheet,
    "clear_reminders": clear_reminders,
    "reset_everything": reset_everything,
    "recall": recall,
    "remember_about_me": remember_about_me,
    "set_my_place": set_my_place,
    "my_place": my_place,
    "my_profile": my_profile,
    "forget_about_me": forget_about_me,
    "web_search": web_search,
    "read_page": read_page,
    "undo_last": undo_last,
    "list_recent_changes": list_recent_changes,
    "show_plan": show_plan,
    "add_to_plan": add_to_plan,
    "edit_plan_item": edit_plan_item,
    "remove_plan_item": remove_plan_item,
    "reopen_item": reopen_item,
    "what_now": what_now,
    "log_progress": log_progress,
    "check_habit": check_habit,
    "list_open_tasks": list_open_tasks,
    "complete_task": complete_task,
    "update_task": update_task,
    "drop_task": drop_task,
    "set_reminder": set_reminder,
    "check_time_free": check_time_free,
    "day_plan": day_plan,
    "plan_gaps": plan_gaps,
    "list_reminders": list_reminders,
    "cancel_reminder": cancel_reminder,
    "save_password": save_password,
    "get_password": get_password,
    "list_passwords": list_passwords,
    "delete_password": delete_password,
    "list_transactions": list_transactions,
    "edit_transaction": edit_transaction,
    "delete_transaction": delete_transaction,
    "edit_reminder": edit_reminder,
    "add_habit": add_habit,
    "list_habits": list_habits,
    "remove_habit": remove_habit,
    "edit_bill_account": edit_bill_account,
    "delete_bill_account": delete_bill_account,
    "remove_mailbox": remove_mailbox,
    "remove_google_account": remove_google_account,
    "set_default_google_account": set_default_google_account,
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
    "write_note": write_note,
    "append_note": append_note,
    "read_note": read_note,
    "search_notes": search_notes,
    "list_notes": list_notes,
    "note_backlinks": note_backlinks,
    "daily_note": daily_note,
    "capture_research": capture_research,
    "review_inbox": review_inbox,
    "vault_status": vault_status,
    "sync_vault": sync_vault,
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

    _fn("add_tasks", "Add one or MANY tasks at once. When the user dumps several problems/jobs in one message, split it and pass every one of them here in a single call — never drop any.",
        {"tasks": {"type": "array", "description": "One entry per job to track.",
                   "items": {"type": "object", "properties": {
                       "title": {"type": "string", "description": "Short action, e.g. 'Call bank about the 5000'."},
                       "notes": {"type": "string", "description": "Any detail worth keeping. Optional."},
                       "priority": {"type": "integer", "description": "1 urgent, 2 normal (default), 3 whenever."},
                       "due_iso": {"type": "string", "description": "Local ISO datetime if they gave a deadline, e.g. 2026-07-31T18:00:00. Omit if none."},
                   }, "required": ["title"]}}},
        ["tasks"]),
    _fn("recall", "Search the user's OWN past messages for something said earlier — a decision, a number, a name, a promise. Use it whenever they refer to something from before that isn't in the visible conversation ('what did I say about', 'that thing last week', 'the price we agreed').",
        {"about": {"type": "string", "description": "Word or phrase to look for."},
         "limit": {"type": "integer", "description": "How many hits. Default 12."}},
        ["about"]),
    _fn("remember_about_me", "Save a durable fact about WHO THIS USER IS — their job/field, what they're building, what they're training for, a constraint (shift hours, exam date, kids), a strong preference. Call this whenever they reveal something lasting, so you fit them in future chats. Not for one-off details.",
        {"fact": {"type": "string", "description": "One short line, e.g. 'Runs a hardware shop in Ajmer' or 'Training for a marathon in Nov'."},
         "replace": {"type": "boolean", "description": "True to replace the whole profile. Default false (append)."}},
        ["fact"]),
    _fn("web_search", "Search the live web. Use it whenever the answer depends on something current or checkable rather than something you already know for certain: prices, free-tier limits, library or runtime versions, docs, error messages, release notes, news, 'is X still true'. Prefer searching over hedging — but if the user only wants your opinion or your reasoning, just answer.",
        {"query": {"type": "string", "description": "What to search for. Write it like a search query, not a sentence."},
         "count": {"type": "integer", "description": "How many results. Default 5, max 10."}},
        ["query"]),
    _fn("read_page", "Open one URL and read its actual text. ALWAYS do this before you state a specific number, price, version, limit, config value or command that came from a search — search snippets are truncated and often stale or wrong. Also use it when the user pastes a link and asks what it says.",
        {"url": {"type": "string", "description": "The full http(s) URL."},
         "max_chars": {"type": "integer", "description": "How much text to pull back. Default 6000."}},
        ["url"]),
    _fn("undo_last", "REVERSE a recent change to the plan, tasks, habits, reminders or profile. Use the moment the user says 'undo', 'undo that', 'revert', 'put it back', 'that was wrong', 'I didn't want that'. It restores the exact state from before the change, so it correctly reverses even a clear_plan or an add_plan that replaced a track. Does NOT touch money — for a wrong transaction use undo_last_transaction instead.",
        {"steps": {"type": "integer", "description": "1 = the newest change (default). 2 = the one before it, and so on. Call list_recent_changes first if they mean something further back."}}),
    _fn("list_recent_changes", "Show the recent changes you've made to their plan/reminders, newest first, as an undo menu. Use for 'what did you just change', 'what did you do', or before undoing something further back than the last action.",
        {"limit": {"type": "integer", "description": "How many to list. Default 8."}}),
    _fn("set_my_place", "Save WHERE THIS USER LIVES. Call it as soon as they mention their city, country or timezone ('I'm in Dubai', 'I moved to Berlin', 'set my timezone to IST'), and whenever a time they gave clearly doesn't match the timezone you're using. Each user of this bot lives somewhere different — never assume one place for everyone.",
        {"timezone": {"type": "string", "description": "IANA name, e.g. Asia/Kolkata, Asia/Dubai, Europe/Berlin, America/New_York."},
         "country": {"type": "string", "description": "e.g. India, UAE, Germany."},
         "currency": {"type": "string", "description": "ISO code, e.g. INR, AED, EUR, USD."},
         "checkin_hour": {"type": "integer", "description": "Local hour (0-23) for the evening 'is it done?' message. Default 21."}}),
    _fn("my_place", "Show the timezone, country, currency and check-in hour currently used for this user. Use when they ask what timezone you're using, or if times look wrong.", {}),
    _fn("my_profile", "Show what you currently know about this user.", {}),
    _fn("forget_about_me", "Remove a remembered fact, or clear the profile if no fact is given.",
        {"fact": {"type": "string", "description": "Words from the line to drop. Omit to clear everything."}}),

    _fn("add_plan", "Store a whole STRUCTURED PLAN as a tree. Use when the user gives a roadmap, phases, or a multi-part goal. Build tracks (big areas like DSA / Dev / Life) -> phases -> tasks, and put repeating things (workout, posting, reading) as kind 'habit'.",
        {"plan": {"type": "array", "description": "Top-level tracks. Each may nest children.",
                  "items": {"type": "object", "properties": {
                      "title": {"type": "string"},
                      "kind": {"type": "string", "enum": ["track", "phase", "task", "habit"]},
                      "track": {"type": "string", "description": "Area name, inherited by children."},
                      "notes": {"type": "string", "description": "Resources, stack, rules."},
                      "gate": {"type": "string", "description": "What proves it's finished, in the user's words."},
                      "priority": {"type": "integer", "description": "1 urgent, 2 normal, 3 whenever."},
                      "target": {"type": "integer", "description": "Countable goal, e.g. 45 problems."},
                      "recur": {"type": "string", "description": "For habits: daily, weekly, 4x_week."},
                      "due_iso": {"type": "string"},
                      "children": {"type": "array", "items": {"type": "object"},
                                   "description": "Nested nodes, same shape."},
                  }, "required": ["title"]}},
         "replace": {"type": "boolean", "description": "TRUE when the user is redoing/correcting their plan — wipes the old one first so tracks don't stack up. Default false."}},
        ["plan"]),
    _fn("disconnect_sheet", "Unlink a connected Google Sheet from the bot. The spreadsheet itself is NOT deleted — only the link.",
        {"name": {"type": "string", "description": "Part of the sheet's title. Omit to disconnect ALL of them."}}),
    _fn("clear_reminders", "Delete all of the user's reminders, including repeating ones.", {}),
    _fn("reset_everything", "Fresh start: wipe the user's plan, tasks, reminders, profile and sheet links. ALWAYS call it first without confirm to show what will go, get the user's yes, then call again with confirm=true.",
        {"confirm": {"type": "boolean", "description": "Only true after the user has explicitly agreed."}}),
    _fn("clear_plan", "Delete a track and everything under it, or the whole plan. Use when the user says remove/delete/clear/start over with their plan. This is their own local data — deleting it is allowed.",
        {"track": {"type": "string", "description": "Track name to remove, e.g. 'Life'. Omit to clear the ENTIRE plan."}}),
    _fn("show_plan", "Show the plan. SHORT by default — one line per track with progress and the phase in play. Pass track to open one area's phases, or full=true only when the user explicitly asks for the whole thing.",
        {"track": {"type": "string", "description": "Open one area, e.g. 'DSA'."},
         "full": {"type": "boolean", "description": "True ONLY if they asked for the full/complete plan. It is long."}}),
    _fn("add_to_plan", "ADD one or more phases/tasks/habits UNDER something that already exists, keeping everything else in place. Use this for 'add a phase to my DSA track', 'put these 3 tasks under P5', 'add a habit to Life'. NEVER use add_plan for an addition — add_plan replaces the whole same-named track and would destroy the other phases and their progress.",
        {"parent": {"type": "string", "description": "Words from the track/phase it goes under, e.g. 'DSA' or 'P5 Async'."},
         "items": {"type": "array", "description": "The new nodes.",
                   "items": {"type": "object", "properties": {
                       "title": {"type": "string"},
                       "kind": {"type": "string", "enum": ["phase", "task", "habit"]},
                       "notes": {"type": "string"},
                       "gate": {"type": "string", "description": "What proves it's finished."},
                       "target": {"type": "integer", "description": "Countable goal, e.g. 45 problems."},
                       "recur": {"type": "string", "description": "For habits: daily, weekly, 4x_week."},
                       "priority": {"type": "integer"},
                       "due_iso": {"type": "string"},
                       "children": {"type": "array", "items": {"type": "object"}},
                   }, "required": ["title"]}}},
        ["parent", "items"]),
    _fn("edit_plan_item", "CHANGE one existing plan item in place — its title, notes, GATE, target, progress, recurrence, priority or status. Use for 'the P3 gate should be X', 'make DSA P1 60 problems', 'rename that phase', 'I'm actually at 12 problems'. Pass ONLY the fields that change. Never re-send the whole plan to fix one line — that wipes recorded progress.",
        {"item_id": {"type": "integer", "description": "The #number if you know it."},
         "title": {"type": "string", "description": "Words from the item's current name, if no id."},
         "new_title": {"type": "string", "description": "Only if renaming it."},
         "notes": {"type": "string"},
         "gate": {"type": "string", "description": "New definition of done."},
         "target": {"type": "integer"},
         "progress": {"type": "integer", "description": "Set the count outright (log_progress adds to it instead)."},
         "recur": {"type": "string"},
         "priority": {"type": "integer"},
         "status": {"type": "string", "enum": ["open", "done", "dropped"]},
         "due_iso": {"type": "string"}}),
    _fn("remove_plan_item", "DELETE one phase/task/habit and anything under it, leaving the rest of the plan alone. Use for 'drop P8 from DSA', 'remove that habit'. Use clear_plan only when they want a WHOLE track or the entire plan gone.",
        {"item_id": {"type": "integer"},
         "title": {"type": "string", "description": "Words from the item to remove."}}),
    _fn("reopen_item", "Put a wrongly completed or dropped item back to open. Use for 'I marked that done by mistake', 'undo that', 'P2 isn't actually finished'.",
        {"item_id": {"type": "integer"},
         "title": {"type": "string", "description": "Words from the item."}}),
    _fn("what_now", "Decide the ONE next thing to do from the plan, plus anything overdue and habits not done today. Use for 'what now?', 'I'm free', 'what should I do'.", {}),
    _fn("log_progress", "Record countable progress on an item, e.g. 'solved 5 problems'. Clears the item automatically when it hits its target.",
        {"task_id": {"type": "integer"}, "title": {"type": "string", "description": "A word from the item, if no id."},
         "count": {"type": "integer", "description": "How many to add. Default 1."}}),
    _fn("check_habit", "Tick off a repeating habit for today (calisthenics, X post, reading) and keep the streak.",
        {"title": {"type": "string", "description": "A word from the habit's name."}}, ["title"]),

    _fn("list_open_tasks", "List the user's open/pending tasks. Use when they ask what's pending, what's left, or what's due.",
        {"when": {"type": "string", "enum": ["all", "today", "overdue"],
                  "description": "'today' = due by tonight, 'overdue' = past due. Default 'all'."}}),
    _fn("complete_task", "Tick a task off once it's done. Give the id, or a word from its title.",
        {"task_id": {"type": "integer", "description": "The #number shown in the list."},
         "title": {"type": "string", "description": "A word or two from the task, if no id."}}),
    _fn("update_task", "Change a task's title, notes, priority or due date.",
        {"task_id": {"type": "integer"}, "title": {"type": "string"},
         "notes": {"type": "string"}, "priority": {"type": "integer"},
         "due_iso": {"type": "string", "description": "New local ISO due datetime."}},
        ["task_id"]),
    _fn("drop_task", "Cancel a task that's no longer needed (it wasn't done).",
        {"task_id": {"type": "integer"}}, ["task_id"]),

    _fn("set_reminder", "Set a time-based reminder, one-off or REPEATING. Compute the exact local datetime from the user's words using the current time given to you. For a daily routine (wake-up, gym, study block) always pass repeat.",
        {"text": {"type": "string", "description": "What to remind about."},
         "when_iso": {"type": "string", "description": "Local ISO datetime of the FIRST firing, e.g. 2026-07-21T17:00:00."},
         "repeat": {"type": "string", "enum": ["daily", "weekdays", "weekends", "weekly"],
                    "description": "Omit for a one-off. 'weekdays' = Mon-Fri."}},
        ["text", "when_iso"]),
    _fn("check_time_free", "Check whether a time slot is free BEFORE you promise it. Call this whenever you're about to place a new time block, or when the user asks 'can I fit X at 7pm'. It reports which existing reminders are within 30 minutes of that time on a day it could share.",
        {"when_iso": {"type": "string", "description": "Local ISO datetime to test, e.g. 2026-07-31T19:30:00."},
         "repeat": {"type": "string", "enum": ["daily", "weekdays", "weekends", "weekly"],
                    "description": "The repeat the new block would have. Omit for a one-off."},
         "window_min": {"type": "integer", "description": "How close counts as a clash. Default 30."}},
        ["when_iso"]),
    _fn("day_plan", "Show one day's timeline — every reminder that fires that day in order, with the free gaps between them marked. Use for 'what does my Tuesday look like', 'where can I fit 2 hours', or before rearranging someone's routine. Answer scheduling questions from THIS, never from memory.",
        {"day": {"type": "string", "description": "'today', 'tomorrow', or a weekday name like 'saturday'. Default today."}}),
    _fn("plan_gaps", "Audit the plan for real holes and report facts: open phases with no gate, empty tracks, phases stalled at zero progress for 2+ weeks, overdue items, habits gone cold, the same phase duplicated in two tracks, and reminders sitting on top of each other. Use for 'what am I missing', 'where are the gaps', 'audit my plan', 'am I on track', or during a weekly review.", {}),
    _fn("list_reminders", "List the user's pending reminders.", {}),
    _fn("cancel_reminder", "Cancel a reminder, by id OR by words from it (match).",
        {"reminder_id": {"type": "integer"},
         "match": {"type": "string", "description": "Words from the reminder, e.g. 'gym'."}}),

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

    _fn("list_transactions", "List recent money entries WITH their id numbers, so a specific one can be corrected or deleted.",
        {"limit": {"type": "integer", "description": "How many. Default 10."}}),
    _fn("edit_transaction", "Correct ANY logged entry. Identify it by id OR by how the user described it (match), e.g. 'the electricity one'. Don't ask for a number you can work out yourself.",
        {"transaction_id": {"type": "integer", "description": "Its #id, if you know it."},
         "match": {"type": "string", "description": "Words from the entry - category, note or amount."},
         "amount": {"type": "number"}, "kind": {"type": "string", "enum": ["in", "out"]},
         "category": {"type": "string"}, "note": {"type": "string"}}),
    _fn("delete_transaction", "Delete a logged entry, by id OR by description (match), e.g. 'the 5000 rent one'.",
        {"transaction_id": {"type": "integer"},
         "match": {"type": "string", "description": "Words from the entry."}}),
    _fn("edit_reminder", "Change a reminder: wording, time, or how it repeats. Identify it by id OR by words from it (match), e.g. 'the gym one'. Pass repeat='none' to stop it repeating.",
        {"reminder_id": {"type": "integer"},
         "match": {"type": "string", "description": "Words from the reminder."},
         "text": {"type": "string"},
         "when_iso": {"type": "string", "description": "New local ISO datetime."},
         "repeat": {"type": "string", "description": "daily | weekdays | weekends | weekly | none"}}),
    _fn("add_habit", "Start tracking a repeating habit with a streak (gym, posting, reading).",
        {"title": {"type": "string"},
         "recur": {"type": "string", "description": "daily | weekly | 4x_week. Default daily."},
         "notes": {"type": "string"}}, ["title"]),
    _fn("list_habits", "Show habits, their streaks, and whether each is done today.", {}),
    _fn("remove_habit", "Stop tracking a habit.", {"title": {"type": "string"}}, ["title"]),
    _fn("edit_bill_account", "Change a tracked bill's name or due/statement day.",
        {"name": {"type": "string"}, "new_name": {"type": "string"},
         "due_day": {"type": "integer"}, "statement_day": {"type": "integer"}}, ["name"]),
    _fn("delete_bill_account", "Stop tracking a bill.", {"name": {"type": "string"}}, ["name"]),
    _fn("remove_mailbox", "Disconnect an IMAP/SMTP mailbox.", {"email": {"type": "string"}}, ["email"]),
    _fn("remove_google_account", "Unlink a Google account.", {"email": {"type": "string"}}, ["email"]),
    _fn("set_default_google_account", "Set which linked Google account is used by default.",
        {"email": {"type": "string"}}, ["email"]),
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

    _fn("write_note", "Write a markdown note into the user's Obsidian vault — anything worth keeping as writing rather than as a task: what they learned, a decision and why, a book's argument, a design, a debugging post-mortem. Link related notes inline with [[Note Title]] so the vault builds a graph instead of a pile. Use their own words and keep the note self-contained.",
        {"title": {"type": "string", "description": "The note's name — this is also its filename and what [[links]] point at. Short and specific: 'Sliding Window', 'Why the p95 dropped 60%'."},
         "content": {"type": "string", "description": "Markdown body. Use [[Other Note]] to link, #tags to categorise."},
         "folder": {"type": "string", "description": "Vault folder, e.g. 'DSA', 'Dev', 'Books'. Optional."},
         "tags": {"type": "array", "items": {"type": "string"}, "description": "Extra tags. Optional."},
         "mode": {"type": "string", "enum": ["replace", "append", "prepend"], "description": "Default 'replace'. Use 'append' to add to an existing note."}},
        ["title", "content"]),
    _fn("append_note", "Add to the end of an existing note without rewriting it (creates it if new). Prefer this over write_note when they're adding to something that already exists.",
        {"title": {"type": "string"}, "content": {"type": "string"},
         "folder": {"type": "string", "description": "Only used if the note is new."}},
        ["title", "content"]),
    _fn("read_note", "Read one note back, with what it links to and what links to it. Use it before rewriting a note, and whenever they ask what they wrote about something.",
        {"note": {"type": "string", "description": "Title, path, or roughly what they call it."}}, ["note"]),
    _fn("search_notes", "Search the vault BY MEANING (embeddings), not just keywords. Use this whenever they ask what they know/wrote/decided about something — their notes are the truth, don't answer from memory.",
        {"query": {"type": "string"},
         "limit": {"type": "integer", "description": "How many notes. Default 6."}}, ["query"]),
    _fn("list_notes", "List notes, newest first, optionally in one folder.",
        {"folder": {"type": "string", "description": "e.g. 'DSA'. Omit for all."},
         "limit": {"type": "integer", "description": "Default 20."}}),
    _fn("note_backlinks", "Show which notes link to a given note — use it to find what a topic connects to before writing more.",
        {"title": {"type": "string"}}, ["title"]),
    _fn("daily_note", "Today's daily note in the vault. Call with no content to read it; with content to add a line (progress, what broke, a number, a decision). This is the running log of the day.",
        {"content": {"type": "string", "description": "Line to add. Omit to read the note."},
         "heading": {"type": "string", "description": "Group it under a heading, e.g. 'DSA', 'Dev', 'Read'. Optional."},
         "day": {"type": "string", "description": "YYYY-MM-DD for a past day. Default today."}}),
    _fn("capture_research", "Park an off-topic idea/urge in the research inbox in one line, so they can keep working. Use this the moment they wander off the topic they're on — capture it, name what they're supposed to be doing, and move on. Do NOT research it now.",
        {"text": {"type": "string", "description": "The urge, in their words."},
         "doing": {"type": "string", "description": "What they're supposed to be on right now. Optional but useful."}},
        ["text"]),
    _fn("review_inbox", "Read the parked research inbox (their Sunday review), or tick items off once read.",
        {"action": {"type": "string", "enum": ["list", "clear"], "description": "Default 'list'."},
         "item": {"type": "string", "description": "With action='clear': words from the one item to close. Omit to close all."}}),
    _fn("vault_status", "Where notes are being saved and whether that link works.", {}),
    _fn("sync_vault", "Pull in notes the user edited in Obsidian and push anything that hasn't reached the vault. Use it when they say they wrote something in Obsidian, or when a note seems out of date.", {}),
]
