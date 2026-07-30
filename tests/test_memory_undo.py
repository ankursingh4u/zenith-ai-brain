"""Undo journal + semantic memory fallback, against a throwaway DB."""
import json, os, sys, tempfile
from datetime import datetime, timedelta

tmp = os.path.join(tempfile.mkdtemp(), "t.db")
os.environ["DATABASE_URL"] = f"sqlite:///{tmp}"
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "x")
os.environ.setdefault("OPENAI_API_KEY", "x")
# No real endpoint here: this must prove memory degrades instead of breaking.
os.environ["LLM_BASE_URL"] = "http://127.0.0.1:9/v1"
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from cryptography.fernet import Fernet
os.environ["ENCRYPTION_KEY"] = Fernet.generate_key().decode()

import config, db
from brain import agent, memory, tools

db.init_db()
UID = 555
db.get_or_create_user(UID, "Test")
fails = []


def check(label, cond, detail=""):
    print(("  ok  " if cond else "  FAIL") + f"  {label}" + (f"  <- {detail}" if not cond and detail else ""))
    if not cond:
        fails.append(label)


def act(tool_name, **kwargs):
    """Run a tool exactly the way the agent does â€” snapshot, run, journal."""
    snap = json.dumps(db.snapshot_user(UID)) if tool_name in agent.MUTATING else None
    result = tools.TOOLS[tool_name](UID, **kwargs)
    if snap and not str(result).startswith("Error running"):
        db.log_action(UID, tool_name, agent._summarise(tool_name, kwargs), snap,
                      config.UNDO_HISTORY)
    return result


print("\n1. Build a plan and record real progress")
act("add_plan", plan=[{"title": "DSA", "kind": "track", "children": [
    {"title": "P1 Linear", "kind": "phase", "target": 45, "gate": "pattern in 60s"},
    {"title": "P2 Search", "kind": "phase", "target": 25, "gate": "clean bounds"},
    {"title": "P3 Stack", "kind": "phase", "target": 30, "gate": "histogram cold"}]}])
act("log_progress", title="P1 Linear", count=12)
act("set_reminder", text="DSA 1 hour", when_iso="2026-08-01T07:30:00", repeat="daily")
check("3 phases + 12 progress", len(db.children(UID, db.tracks(UID)[0].id)) == 3
      and db.find_nodes(UID, "P1 Linear")[0].progress == 12)

print("\n2. The worst case: add_plan wipes the track. Undo must bring it ALL back")
act("add_plan", plan=[{"title": "DSA", "kind": "track", "children": [
    {"title": "P1 Linear", "kind": "phase", "target": 45}]}])
check("damage done (1 phase, progress lost)",
      len(db.children(UID, db.tracks(UID)[0].id)) == 1
      and db.find_nodes(UID, "P1 Linear")[0].progress == 0)
print(act("undo_last"))
kids = db.children(UID, db.tracks(UID)[0].id)
check("all 3 phases restored", len(kids) == 3, f"got {len(kids)}")
check("progress restored to 12", db.find_nodes(UID, "P1 Linear")[0].progress == 12,
      str(db.find_nodes(UID, "P1 Linear")[0].progress))
check("gates restored", db.find_nodes(UID, "P3 Stack")[0].gate == "histogram cold")
check("parent links still valid", all(k.parent_id == db.tracks(UID)[0].id for k in kids))
check("reminder untouched by the plan undo", len(db.list_reminders(UID)) == 1)

print("\n3. Undo a clear_plan (the other destructive one)")
act("clear_plan")
check("plan gone", not db.tracks(UID))
act("undo_last")
check("whole plan back", len(db.children(UID, db.tracks(UID)[0].id)) == 3)

print("\n4. Undo a wrong single edit, and a wrong deletion")
act("edit_plan_item", title="P2 Search", gate="WRONG GATE", target=999)
check("edit applied", db.find_nodes(UID, "P2 Search")[0].target == 999)
act("undo_last")
p2 = db.find_nodes(UID, "P2 Search")[0]
check("edit reversed", p2.gate == "clean bounds" and p2.target == 25,
      f"{p2.gate}/{p2.target}")
act("remove_plan_item", title="P3 Stack")
check("phase removed", len(db.children(UID, db.tracks(UID)[0].id)) == 2)
act("undo_last")
check("phase restored", len(db.children(UID, db.tracks(UID)[0].id)) == 3)

print("\n5. Undo reaches back several steps")
act("edit_plan_item", title="P1 Linear", new_title="P1 RENAMED ONE")
act("edit_plan_item", title="P2 Search", new_title="P2 RENAMED TWO")
act("edit_plan_item", title="P3 Stack", new_title="P3 RENAMED THREE")
print(tools.list_recent_changes(UID, 3))
print(act("undo_last", steps=3))
titles = sorted(k.title for k in db.children(UID, db.tracks(UID)[0].id))
check("all three renames reversed at once",
      titles == ["P1 Linear", "P2 Search", "P3 Stack"], str(titles))

print("\n6. Reminders and habits are undoable too")
act("set_reminder", text="Oops wrong block", when_iso="2026-08-01T15:00:00", repeat="daily")
check("reminder added", len(db.list_reminders(UID)) == 2)
act("undo_last")
check("reminder removed by undo", len(db.list_reminders(UID)) == 1)
act("add_habit", title="Calisthenics", recur="4x_week")
act("check_habit", title="Calisthenics")
check("streak is 1", db.habits(UID)[0].streak == 1)
act("undo_last")
check("streak rolled back to 0", db.habits(UID)[0].streak == 0,
      str(db.habits(UID)[0].streak))

print("\n7. Profile edits are undoable")
act("remember_about_me", fact="I am a rocket scientist")
check("fact stored", "rocket" in (db.get_profile(UID) or ""))
act("undo_last")
check("fact reversed", "rocket" not in (db.get_profile(UID) or ""))

print("\n8. Money is deliberately NOT swept up in a plan undo")
tools.log_transaction(UID, 450, "out", "food")
before = len(db.list_transactions(UID))
act("edit_plan_item", title="P1 Linear", notes="x")
act("undo_last")
check("transaction survives a plan undo", len(db.list_transactions(UID)) == before)

print("\n9. Undo is bounded and honest")
check("asking too far back is refused, not faked",
      "only have" in act("undo_last", steps=99).lower())
fresh = 556
db.get_or_create_user(fresh, "Fresh")
check("nothing to undo says so", "Nothing to undo" in tools.undo_last(fresh))
check("journal trimmed to the cap",
      len(db.recent_actions(UID, 50)) <= config.UNDO_HISTORY,
      str(len(db.recent_actions(UID, 50))))

print("\n10. Users cannot touch each other's undo history")
other = db.snapshot_user(fresh)
check("other user's snapshot is empty", not other["tasks"])
check("mark_undone refuses a foreign id",
      db.mark_undone(fresh, db.recent_actions(UID, 1)[0].id) is False)

print("\n11. Semantic memory degrades instead of breaking (no endpoint here)")
check("remember returns False, no crash", memory.remember(UID, "a" * 60) is False)
check("search returns [], no crash", memory.search(UID, "caching") == [])
check("available() flips off after failure", memory.available() is False)
db.save_turn(UID, "user", "I decided to use Redis cache-aside on three endpoints")
out = tools.recall(UID, "Redis")
check("keyword recall still works as the fallback", "Redis" in out, out[:80])
check("and it says so honestly", "keyword search only" in out, out[:120])

print("\n12. Cosine maths is right (the part that has no endpoint dependency)")
check("identical vectors score 1", abs(memory._cosine([1, 0, 1], [1, 0, 1]) - 1.0) < 1e-9)
check("orthogonal vectors score 0", abs(memory._cosine([1, 0], [0, 1])) < 1e-9)
check("length mismatch is safe", memory._cosine([1, 0], [1, 0, 0]) == 0.0)
check("empty is safe", memory._cosine([], []) == 0.0)

print("\n13. Nothing else regressed")
check("registry and schemas agree",
      {s["function"]["name"] for s in tools.SCHEMAS} == set(tools.TOOLS))
check("show_plan works", "DSA" in tools.show_plan(UID))
check("what_now works", len(tools.what_now(UID)) > 10)
check("plan_gaps works", len(tools.plan_gaps(UID)) > 10)
check("day_plan works", any(d in tools.day_plan(UID, "tomorrow") for d in
      ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")))
check("summary works", "450" in tools.get_summary(UID))

print("\n" + ("ALL PASSED" if not fails else f"{len(fails)} FAILED: {fails}"))
sys.exit(1 if fails else 0)

