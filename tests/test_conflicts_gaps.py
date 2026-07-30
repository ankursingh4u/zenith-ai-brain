"""Conflict detection + plan audit, against a throwaway DB.

Assertions match on wording, not emoji: emoji literals in a test file are one
bad shell redirect away from becoming mojibake and failing for no real reason.
"""
import os, sys, tempfile
from datetime import datetime, timedelta

tmp = os.path.join(tempfile.mkdtemp(), "t.db")
os.environ["DATABASE_URL"] = f"sqlite:///{tmp}"
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "x")
os.environ.setdefault("OPENAI_API_KEY", "x")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from cryptography.fernet import Fernet
os.environ["ENCRYPTION_KEY"] = Fernet.generate_key().decode()

import db
from brain import tools

db.init_db()
UID = 777
db.get_or_create_user(UID, "Test")
fails = []
TOMORROW = (datetime.now(tools._TZ) + timedelta(days=1)).date().isoformat()


def check(label, cond, detail=""):
    print(("  ok  " if cond else "  FAIL") + f"  {label}" + (f"  <- {detail}" if not cond and detail else ""))
    if not cond:
        fails.append(label)


print("\n1. Daily routine goes in cleanly (no false clashes)")
for t, txt in [("07:30", "DSA 1 hour"), ("20:30", "Calisthenics"),
               ("21:45", "Dev phase work"), ("23:40", "Philosophy")]:
    out = tools.set_reminder(UID, txt, f"{TOMORROW}T{t}:00", "daily")
    check(f"{t} {txt} set with no false clash", "Heads up" not in out, out[-90:])

print("\n2. A real clash is caught when adding")
out = tools.set_reminder(UID, "Client call", f"{TOMORROW}T21:50:00", "daily")
check("clash detected on insert", "Heads up" in out and "Dev phase" in out, out[-120:])
check("gap distance reported", "5 min away" in out, out[-120:])

print("\n3. check_time_free before promising")
busy = tools.check_time_free(UID, f"{TOMORROW}T07:45:00", "daily")
free = tools.check_time_free(UID, f"{TOMORROW}T17:00:00", "daily")
check("busy slot flagged", "clashes with" in busy and "DSA" in busy, busy[:90])
check("free slot cleared", "looks free" in free, free[:90])

print("\n4. Repeat patterns that can never share a day are NOT a clash")
tools.set_reminder(UID, "Weekend deep work", f"{TOMORROW}T07:35:00", "weekends")
sat = tools.check_time_free(UID, f"{TOMORROW}T07:35:00", "weekends")
check("weekends vs weekends at the same time DOES clash", "clashes with" in sat, sat[:70])
# A Saturday-only block against a weekdays-only block can never collide.
tools.set_reminder(UID, "Weekday standup", f"{TOMORROW}T11:00:00", "weekdays")
no = tools.check_time_free(UID, f"{TOMORROW}T11:00:00", "weekends")
check("weekdays vs weekends is NOT a clash", "looks free" in no, no[:90])

print("\n5. Midnight wrap-around is measured correctly")
tools.set_reminder(UID, "Teardown check", f"{TOMORROW}T23:55:00", "daily")
wrap = tools.check_time_free(UID, f"{TOMORROW}T00:05:00", "daily", 30)
check("23:55 and 00:05 seen as 10 min apart",
      "clashes with" in wrap and "10 min" in wrap, wrap[:110])

print("\n6. day_plan shows the timeline and the free gaps")
tl = tools.day_plan(UID, "tomorrow")
check("timeline ordered earliest first", tl.index("07:30") < tl.index("21:45"), "")
check("free gaps marked", "free)" in tl, tl[:200])
check("the day is named", any(d in tl for d in
      ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")), tl[:60])
print(tl)

print("\n7. plan_gaps on an empty plan")
check("empty plan says so", "No plan stored" in tools.plan_gaps(UID))

print("\n8. plan_gaps finds real holes")
tools.add_plan(UID, [
    {"title": "DSA", "kind": "track", "children": [
        {"title": "P1 Linear", "kind": "phase", "target": 45, "gate": "pattern in 60s"},
        {"title": "P2 Search", "kind": "phase", "target": 25},          # no gate
    ]},
    {"title": "Dev", "kind": "track", "children": [
        {"title": "P1 Linear", "kind": "phase", "gate": "dup title test"},  # duplicate
    ]},
    {"title": "Web3", "kind": "track"},                                  # empty track
])
tools.add_to_plan(UID, "DSA", [{"title": "Calisthenics", "kind": "habit", "recur": "4x_week"}])
report = tools.plan_gaps(UID)
print(report)
check("missing gate found", "No gate" in report and "P2 Search" in report, "")
check("empty track found", "Web3" in report and "empty" in report, "")
check("duplicate found", "two places" in report.lower(), "")
check("never-ticked habit found", "never ticked" in report, "")
check("reminder clash found", "on top of each other" in report, "")

print("\n9. Stalled detection uses real age, so a fresh phase is not flagged")
check("fresh phase not called stalled", "Stalled" not in report, "")
with db.session() as s:
    from db import Task
    row = s.get(Task, db.find_nodes(UID, "P1 Linear")[0].id)
    row.created_at = datetime.utcnow() - timedelta(days=40)
    s.commit()
check("40-day-old zero-progress phase IS stalled", "Stalled" in tools.plan_gaps(UID))

print("\n10. A clean plan reports clean")
UID2 = 778
db.get_or_create_user(UID2, "Clean")
tools.add_plan(UID2, [{"title": "DSA", "kind": "track", "children": [
    {"title": "P1", "kind": "phase", "target": 45, "gate": "pattern in 60s"}]}])
clean = tools.plan_gaps(UID2)
check("no false positives on a clean plan", "No structural gaps" in clean, clean[:120])

print("\n11. Nothing else regressed")
check("registry and schemas still agree",
      {s["function"]["name"] for s in tools.SCHEMAS} == set(tools.TOOLS))
check("list_reminders works", "#" in tools.list_reminders(UID))
check("what_now works", len(tools.what_now(UID)) > 10)
check("money still fine", "450" in tools.log_transaction(UID, 450, "out", "food"))

print("\n" + ("ALL PASSED" if not fails else f"{len(fails)} FAILED: {fails}"))
sys.exit(1 if fails else 0)
