"""Scheduled automation: reminders and due-date nudges.

- Every minute: fire any reminders whose time has arrived.
- Once a day: remind about bills whose due date is near.

Everything is per-user and sent over Telegram.
"""
from __future__ import annotations

import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

import config
import db

log = logging.getLogger("brain.scheduler")


def _now():
    return datetime.now(ZoneInfo(config.TIMEZONE))


async def _notify(bot, telegram_id: int, text: str) -> None:
    try:
        await bot.send_message(chat_id=telegram_id, text=text)
    except Exception as e:  # noqa: BLE001
        log.warning("Telegram notify failed for %s: %s", telegram_id, e)


async def fire_due_reminders(bot) -> None:
    """Runs every minute: send any reminders whose time has come."""
    for r in db.due_reminders(datetime.utcnow()):
        name = (db.get_user_name(r.telegram_id) or "").split(" ")[0]
        greet = f"Hey {name} 👋 " if name else ""
        await _notify(bot, r.telegram_id, f"{greet}⏰ Reminder: {r.text}")
        db.mark_reminder_fired(r.id)


async def run_daily_check(bot) -> None:
    """Daily: nudge about bills due within DUE_REMINDER_DAYS."""
    today = _now().day
    log.info("Daily bill check running (day %s)...", today)
    for acc in db.accounts_due_soon(today, config.DUE_REMINDER_DAYS):
        diff = acc.due_day - today
        when = "today" if diff == 0 else f"in {diff} day(s)"
        await _notify(bot, acc.telegram_id,
                      f"⏰ Reminder: {acc.name} bill is due {when} (day {acc.due_day}).")


def _overdue_phrase(t, now_local, tz) -> str:
    """How late this is, in words — 'due today' reads very differently to '5 days late'."""
    if t.due_at is None:
        return "no date set"
    due = t.due_at.replace(tzinfo=ZoneInfo("UTC")).astimezone(tz)
    days = (now_local.date() - due.date()).days
    if days > 1:
        return f"{days} days late"
    if days == 1:
        return "1 day late"
    if days == 0:
        return "due today"
    return f"due {due:%a %d %b}"


async def send_task_digest(bot) -> None:
    """Morning: only what's actually live today. Never the whole backlog.

    Dumping every open task trains people to ignore the message. Overdue,
    due-soon and explicitly-urgent items are the only things worth opening
    the day with.
    """
    tz = ZoneInfo(config.TIMEZONE)
    now_local = _now()
    now_utc = datetime.utcnow()
    for uid in db.users_with_open_tasks():
        rows = db.worth_chasing(uid, now_utc)
        if not rows:
            continue                      # nothing live — say nothing
        lines = [f"• {t.title} — {_overdue_phrase(t, now_local, tz)}" for t in rows[:7]]
        extra = f"\n(+{len(rows) - 7} more)" if len(rows) > 7 else ""
        head = ("☀️ 1 thing live today:" if len(rows) == 1
                else f"☀️ {len(rows)} things live today:")
        await _notify(bot, uid, f"{head}\n" + "\n".join(lines) + extra)


async def evening_checkin(bot) -> None:
    """Evening: ASK how it went, rather than listing things again.

    This is the "is it done?" pass. It batches into a single question, chases
    only what qualifies, and escalates its wording the longer something sits —
    a commitment asked about five times is not "still open", it's stuck.
    """
    tz = ZoneInfo(config.TIMEZONE)
    now_local = _now()
    now_utc = datetime.utcnow()
    # "Still open from today" has to mean the whole day, including work due
    # later tonight — otherwise the message contradicts its own wording.
    end_of_day = now_local.replace(hour=23, minute=59, second=59)
    hours_left = max(0, int((end_of_day - now_local).total_seconds() // 3600) + 1)
    for uid in db.users_with_open_tasks():
        rows = db.worth_chasing(uid, now_utc, horizon_hours=hours_left)
        if not rows:
            continue
        stuck = [t for t in rows if (t.nudges or 0) >= 3]
        lines = [f"• {t.title} — {_overdue_phrase(t, now_local, tz)}" for t in rows[:7]]
        n = len(rows)
        head = (f"🌙 {n} things still open from today. How many did you get done?"
                if n > 1 else "🌙 One thing still open from today. Did it get done?")
        tail = ""
        if stuck:
            names = ", ".join(t.title for t in stuck[:3])
            tail = (f"\n\n{names} — I've asked about this {max(t.nudges for t in stuck) + 1} "
                    "times now. Worth either doing it, rescheduling it, or dropping it.")
        await _notify(bot, uid, f"{head}\n" + "\n".join(lines) + tail
                      + "\n\nTell me what's done and I'll tick them off.")
        db.mark_nudged(uid, [t.id for t in rows], now_utc)


def start_scheduler(application) -> AsyncIOScheduler:
    """Start the jobs on the bot's running event loop (called from post_init)."""
    scheduler = AsyncIOScheduler(timezone=config.TIMEZONE)
    scheduler.add_job(
        run_daily_check, trigger=CronTrigger(hour=config.DAILY_JOB_HOUR, minute=0),
        args=[application.bot], id="daily_check", replace_existing=True,
    )
    scheduler.add_job(
        fire_due_reminders, trigger=IntervalTrigger(minutes=1),
        args=[application.bot], id="reminders", replace_existing=True,
    )
    scheduler.add_job(
        send_task_digest, trigger=CronTrigger(hour=config.DAILY_JOB_HOUR, minute=5),
        args=[application.bot], id="task_digest", replace_existing=True,
    )
    scheduler.add_job(
        evening_checkin, trigger=CronTrigger(hour=config.CHECKIN_HOUR, minute=0),
        args=[application.bot], id="evening_checkin", replace_existing=True,
    )
    scheduler.start()
    log.info("Scheduler started — bills %02d:00, morning list %02d:05, "
             "evening check-in %02d:00 (%s), reminders every minute.",
             config.DAILY_JOB_HOUR, config.DAILY_JOB_HOUR, config.CHECKIN_HOUR,
             config.TIMEZONE)
    return scheduler
