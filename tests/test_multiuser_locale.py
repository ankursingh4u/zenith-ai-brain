"""Several users, several countries. Nothing may be global.

The bug this guards: a single module-level timezone and a hardcoded country in
the prompt gave every user the server's clock, rupees, and Hinglish. Someone in
Berlin was being told their money is INR.
"""
import asyncio
import os
import sys
import tempfile
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

tmp = os.path.join(tempfile.mkdtemp(), "t.db")
os.environ["DATABASE_URL"] = f"sqlite:///{tmp}"
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "x")
os.environ.setdefault("OPENAI_API_KEY", "x")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from cryptography.fernet import Fernet
os.environ["ENCRYPTION_KEY"] = Fernet.generate_key().decode()

import db
from brain import agent, tools
from scheduler import jobs

db.init_db()
IN, DE, US = 501, 502, 503
for uid, name in [(IN, "Ankur"), (DE, "Lena"), (US, "Sam")]:
    db.get_or_create_user(uid, name)
fails = []


def check(label, cond, detail=""):
    print(("  ok  " if cond else "  FAIL") + f"  {label}" + (f"  <- {detail}" if not cond and detail else ""))
    if not cond:
        fails.append(label)


class FakeBot:
    def __init__(self):
        self.sent = []

    async def send_message(self, chat_id, text):
        self.sent.append((chat_id, text))


print("\n1. Each user sets their own place")
print("  " + tools.set_my_place(IN, "Asia/Kolkata", "India", "INR"))
print("  " + tools.set_my_place(DE, "Europe/Berlin", "Germany", "EUR"))
print("  " + tools.set_my_place(US, "America/New_York", "United States", "USD"))
check("india stored", db.get_locale(IN)["timezone"] == "Asia/Kolkata")
check("germany stored", db.get_locale(DE)["timezone"] == "Europe/Berlin")
check("us stored", db.get_locale(US)["timezone"] == "America/New_York")
check("currencies are separate",
      {db.get_locale(u)["currency"] for u in (IN, DE, US)} == {"INR", "EUR", "USD"})

print("\n2. The SAME wall-clock time means different UTC per user")
for uid in (IN, DE, US):
    tools.set_reminder(uid, "standup", "2026-09-10T09:00:00", None)
stored = {}
for uid in (IN, DE, US):
    r = db.list_reminders(uid)[0]
    stored[uid] = r.due_at.strftime("%H:%M")
print(f"   9am local -> UTC: India {stored[IN]}, Berlin {stored[DE]}, New York {stored[US]}")
check("three different UTC instants", len(set(stored.values())) == 3, str(stored))
check("india 9am -> 03:30 UTC", stored[IN] == "03:30", stored[IN])
check("berlin 9am -> 07:00 UTC (CEST)", stored[DE] == "07:00", stored[DE])
check("new york 9am -> 13:00 UTC (EDT)", stored[US] == "13:00", stored[US])

print("\n3. ...and each user sees their own 9am read back")
for uid, place in [(IN, "India"), (DE, "Berlin"), (US, "New York")]:
    out = tools.list_reminders(uid)
    check(f"{place} sees 09:00", "09:00" in out, out[:70])

print("\n4. A user's reminders are invisible to the others")
check("one reminder each", all(len(db.list_reminders(u)) == 1 for u in (IN, DE, US)))
# Daily blocks, so they land on every day's timeline regardless of the date.
for uid, label in [(IN, "chai"), (DE, "kaffee"), (US, "coffee")]:
    tools.set_reminder(uid, label, "2026-09-10T16:00:00", "daily")
for uid, mine, theirs in [(IN, "chai", ("kaffee", "coffee")),
                          (DE, "kaffee", ("chai", "coffee")),
                          (US, "coffee", ("chai", "kaffee"))]:
    plan = tools.day_plan(uid, "today")
    check(f"day_plan for {uid} shows only their own block",
          mine in plan and not any(t in plan for t in theirs), plan[:90])

print("\n5. The prompt block is built per user")
blocks = {u: agent._place_block(u) for u in (IN, DE, US)}
check("india block says India", "India" in blocks[IN] and "Asia/Kolkata" in blocks[IN])
check("germany block says Germany", "Germany" in blocks[DE] and "Europe/Berlin" in blocks[DE])
check("germany block does NOT mention rupees/lakh",
      "lakh" not in blocks[DE] and "INR" not in blocks[DE], blocks[DE][:160])
check("germany block does NOT mention Hinglish",
      "subah" not in blocks[DE] and "parso" not in blocks[DE])
check("india block DOES explain lakh and crore",
      "lakh" in blocks[IN] and "crore" in blocks[IN])
check("india block DOES cover Hinglish", "subah" in blocks[IN])
check("us block says USD", "USD" in blocks[US] and "United States" in blocks[US])
check("every block still forbids guessing a bare hour",
      all("BARE HOUR IS AMBIGUOUS" in b for b in blocks.values()))
check("each block carries that user's own current time",
      all(datetime.now(ZoneInfo(db.get_locale(u)["timezone"])).strftime("%H:%M") in blocks[u]
          for u in (IN, DE, US)))

print("\n6. An unknown user is not assumed to be anywhere")
NEW = 504
db.get_or_create_user(NEW, "Unknown")
loc = db.get_locale(NEW)
b = agent._place_block(NEW)
check("falls back to the server default", loc["timezone"] == "Asia/Kolkata")
check("but is flagged as NOT known", loc["known"] is False)
check("prompt admits it wasn't told", "have NOT been told where they are" in b, b[:200])
check("and says to ask, not assume", "which city are you in" in b)
check("a known user has no such disclaimer",
      "have NOT been told" not in blocks[IN])

print("\n7. The evening check-in fires on each user's own clock")
now_utc = datetime.utcnow()
for uid in (IN, DE, US):
    db.add_task(uid, f"job-{uid}", None, 1, now_utc - timedelta(hours=1))
# Pin every user's check-in to the hour it currently is in Berlin only.
berlin_hour = datetime.now(ZoneInfo("Europe/Berlin")).hour
for uid in (IN, DE, US):
    db.set_locale(uid, checkin_hour=berlin_hour)
bot = FakeBot()
asyncio.run(jobs.evening_checkin(bot))
got = {c for c, _ in bot.sent}
india_hour = datetime.now(ZoneInfo("Asia/Kolkata")).hour
ny_hour = datetime.now(ZoneInfo("America/New_York")).hour
check("berlin user is messaged now", DE in got, str(got))
check("india user only if it is also that hour there",
      (IN in got) == (india_hour == berlin_hour), f"IST hour {india_hour} vs {berlin_hour}")
check("new york user only if it is also that hour there",
      (US in got) == (ny_hour == berlin_hour), f"NY hour {ny_hour} vs {berlin_hour}")
for chat_id, text in bot.sent:
    check(f"message to {chat_id} contains only their own task",
          f"job-{chat_id}" in text and not any(
              f"job-{o}" in text for o in (IN, DE, US) if o != chat_id), text[:80])

print("\n8. Changing place updates everything downstream")
tools.set_my_place(DE, "Asia/Dubai", "UAE", "AED")
b2 = agent._place_block(DE)
check("timezone moved", db.get_locale(DE)["timezone"] == "Asia/Dubai")
check("prompt follows", "UAE" in b2 and "Asia/Dubai" in b2 and "AED" in b2)
check("still no rupees for them", "lakh" not in b2)
check("my_place reports it back", "Asia/Dubai" in tools.my_place(DE))

print("\n9. A nonsense timezone is refused, not stored")
before = db.get_locale(US)["timezone"]
out = tools.set_my_place(US, "Mars/Olympus")
check("refused", "isn't a timezone I know" in out, out[:80])
check("nothing changed", db.get_locale(US)["timezone"] == before)

print("\n10. Registry still consistent")
check("schemas and tools agree",
      {s["function"]["name"] for s in tools.SCHEMAS} == set(tools.TOOLS))
check("set_my_place is registered", "set_my_place" in tools.TOOLS)

print("\n" + ("ALL PASSED" if not fails else f"{len(fails)} FAILED: {fails}"))
sys.exit(1 if fails else 0)
