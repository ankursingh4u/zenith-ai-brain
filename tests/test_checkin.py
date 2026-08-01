"""Chasing logic: does it nudge the right things, and stay quiet about the rest?

The failure mode being guarded against is noise - a check-in that lists every
open task teaches you to ignore it.
"""
import asyncio
import os
import sys
import tempfile
from datetime import datetime, timedelta

tmp = os.path.join(tempfile.mkdtemp(), "t.db")
os.environ["DATABASE_URL"] = f"sqlite:///{tmp}"
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "x")
os.environ.setdefault("OPENAI_API_KEY", "x")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from cryptography.fernet import Fernet
os.environ["ENCRYPTION_KEY"] = Fernet.generate_key().decode()

import db
from scheduler import jobs
from datetime import datetime as _dt
from zoneinfo import ZoneInfo as _ZI


def due_now(*uids):
    """Pin these users' check-in hour to whatever hour it is for them now.

    evening_checkin is per-user now: it only messages someone when their own
    local check-in hour comes round, so a test has to say when that is.
    """
    for u in uids:
        tz = _ZI(db.get_locale(u)["timezone"])
        db.set_locale(u, checkin_hour=_dt.now(tz).hour)

db.init_db()
UID = 909
db.get_or_create_user(UID, "Tester")
NOW = datetime.utcnow()
fails = []


def check(label, cond, detail=""):
    print(("  ok  " if cond else "  FAIL") + f"  {label}" + (f"  <- {detail}" if not cond and detail else ""))
    if not cond:
        fails.append(label)


class FakeBot:
    """Captures what would have been sent, so nothing touches Telegram."""
    def __init__(self):
        self.sent = []

    async def send_message(self, chat_id, text):
        self.sent.append((chat_id, text))


print("\n1. Only real commitments get chased")
overdue = db.add_task(UID, "Pitch to ventures", None, 1, NOW - timedelta(days=3))
today = db.add_task(UID, "Send the deck", None, 2, NOW + timedelta(hours=4))
urgent = db.add_task(UID, "Call the CA", None, 1, None)          # p1, no date
someday = db.add_task(UID, "Read DDIA chapter", None, 2, None)   # parked on purpose
far = db.add_task(UID, "Renew domain", None, 2, NOW + timedelta(days=20))

chased = {t.title for t in db.worth_chasing(UID, NOW)}
check("overdue commitment chased", "Pitch to ventures" in chased)
check("due-today item chased", "Send the deck" in chased)
check("priority-1 with no date chased", "Call the CA" in chased)
check("undated normal task NOT chased", "Read DDIA chapter" not in chased, str(chased))
check("far-future task NOT chased", "Renew domain" not in chased, str(chased))

print("\n2. Evening check-in ASKS, batched into one message")
due_now(UID)
bot = FakeBot()
asyncio.run(jobs.evening_checkin(bot))
check("exactly one message sent", len(bot.sent) == 1, f"{len(bot.sent)} messages")
msg = bot.sent[0][1] if bot.sent else ""
print("   " + msg.replace("\n", "\n   ")[:320])
check("asks how many are done", "How many did you get done?" in msg, msg[:80])
check("counts them", "still open from today" in msg)
check("names the overdue one with how late it is", "3 days late" in msg, msg[:200])
check("includes work due later tonight", "Send the deck" in msg, msg[:220])
check("does not include the parked task", "Read DDIA" not in msg)
check("does not include the far-future task", "Renew domain" not in msg)

print("\n3. Asking is recorded, and the wording escalates when stuck")
row = [t for t in db.list_tasks(UID, "open", limit=50) if t.id == overdue][0]
check("nudge counted", row.nudges == 1, str(row.nudges))
check("nudge timestamped", row.last_nudged_at is not None)
for _ in range(3):
    asyncio.run(jobs.evening_checkin(bot))
last = bot.sent[-1][1]
check("escalates after repeated asks", "times now" in last, last[-200:])
check("suggests doing, rescheduling or dropping",
      "rescheduling it, or dropping it" in last, last[-160:])

print("\n4. Completing work stops the chasing")
db.set_task_status(UID, overdue, "done")
db.set_task_status(UID, today, "done")
db.set_task_status(UID, urgent, "done")
bot2 = FakeBot()
asyncio.run(jobs.evening_checkin(bot2))
check("silence when nothing is live", len(bot2.sent) == 0,
      bot2.sent[0][1][:80] if bot2.sent else "")

print("\n5. Morning list is live-work only, not the backlog")
db.add_task(UID, "Ship the invoice", None, 1, NOW + timedelta(hours=2))
bot3 = FakeBot()
asyncio.run(jobs.send_task_digest(bot3))
check("one morning message", len(bot3.sent) == 1, str(len(bot3.sent)))
m = bot3.sent[0][1] if bot3.sent else ""
print("   " + m.replace("\n", "\n   ")[:220])
check("includes the live item", "Ship the invoice" in m)
check("singular reads properly (no '1 thing(s)')", "1 thing live today" in m, m[:60])
check("still excludes the parked backlog", "Read DDIA" not in m, m[:150])

print("\n6. A user with only parked work is left alone entirely")
OTHER = 910
db.get_or_create_user(OTHER, "Quiet")
due_now(OTHER)
db.add_task(OTHER, "Someday: learn Rust", None, 2, None)
bot4 = FakeBot()
asyncio.run(jobs.evening_checkin(bot4))
asyncio.run(jobs.send_task_digest(bot4))
check("no messages at all", not [s for s in bot4.sent if s[0] == OTHER],
      str(bot4.sent))

print("\n7. Singular vs plural reads naturally")
SOLO = 911
db.get_or_create_user(SOLO, "Solo")
due_now(SOLO)
db.add_task(SOLO, "One job", None, 1, NOW - timedelta(hours=2))
bot5 = FakeBot()
asyncio.run(jobs.evening_checkin(bot5))
solo_msg = [t for c, t in bot5.sent if c == SOLO][0]
check("singular phrasing", "One thing still open" in solo_msg and "Did it get done?" in solo_msg,
      solo_msg[:90])

print("\n8. Users are isolated from each other")
# bot5 legitimately carries messages for several users - the job sweeps
# everyone. What must hold is that no message mentions another user's work.
for chat_id, text in bot5.sent:
    if chat_id == SOLO:
        check("SOLO's message has only SOLO's task",
              "One job" in text and "Ship the invoice" not in text, text[:90])
    elif chat_id == UID:
        check("UID's message has only UID's task",
              "Ship the invoice" in text and "One job" not in text, text[:90])
check("the quiet user got nothing", not [c for c, _ in bot5.sent if c == OTHER])
check("every recipient is a real user we created",
      all(c in (UID, OTHER, SOLO) for c, _ in bot5.sent))

print("\n" + ("ALL PASSED" if not fails else f"{len(fails)} FAILED: {fails}"))
sys.exit(1 if fails else 0)
