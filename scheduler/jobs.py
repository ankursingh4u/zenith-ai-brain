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
    scheduler.start()
    log.info("Scheduler started — daily bill check at %02d:00 %s, reminders every minute.",
             config.DAILY_JOB_HOUR, config.TIMEZONE)
    return scheduler
