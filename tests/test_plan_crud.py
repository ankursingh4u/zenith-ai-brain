"""Exercise the new plan CRUD against a throwaway DB. Touches nothing live."""
import os, sys, tempfile

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
UID = 999
db.get_or_create_user(UID, "Test")
fails = []


def check(label, cond, detail=""):
    print(("  ok  " if cond else "  FAIL") + f"  {label}" + (f"  <- {detail}" if not cond and detail else ""))
    if not cond:
        fails.append(label)


print("\n1. Load a track with phases, gates and targets")
print(tools.add_plan(UID, [{
    "title": "DSA", "kind": "track", "children": [
        {"title": "P1 Linear", "kind": "phase", "target": 45, "gate": "pattern in 60s"},
        {"title": "P2 Search", "kind": "phase", "target": 25, "gate": "bug-free bounds"},
        {"title": "P3 Stack", "kind": "phase", "target": 30, "gate": "histogram cold"},
    ]}]))
kids = db.children(UID, db.tracks(UID)[0].id)
check("3 phases stored", len(kids) == 3, f"got {len(kids)}")

print("\n2. Record real progress, then edit ONE gate â€” progress must survive")
tools.log_progress(UID, title="P1 Linear", count=12)
print(tools.edit_plan_item(UID, title="P3 Stack", gate="Solve Largest Rectangle cold, no hints"))
p1 = db.find_nodes(UID, "P1 Linear")[0]
p3 = db.find_nodes(UID, "P3 Stack")[0]
check("P1 progress preserved after editing P3", p1.progress == 12, f"progress={p1.progress}")
check("P3 gate actually changed", "no hints" in (p3.gate or ""), p3.gate)
check("other phases still there", len(db.children(UID, db.tracks(UID)[0].id)) == 3)

print("\n3. Add a phase to the existing track (the old wipe bug)")
print(tools.add_to_plan(UID, "DSA", [
    {"title": "P4 Trees", "kind": "phase", "target": 35, "gate": "recursive to iterative"}]))
kids = db.children(UID, db.tracks(UID)[0].id)
p1 = db.find_nodes(UID, "P1 Linear")[0]
check("now 4 phases", len(kids) == 4, f"got {len(kids)}")
check("progress STILL preserved after an add", p1.progress == 12, f"progress={p1.progress}")
check("appended last, not first", kids[-1].title == "P4 Trees", kids[-1].title)
check("track inherited", kids[-1].track == "DSA", str(kids[-1].track))

print("\n4. Prove the old path would have destroyed it (why add_to_plan exists)")
tools.add_plan(UID, [{"title": "DSA", "kind": "track", "children": [
    {"title": "P1 Linear", "kind": "phase", "target": 45}]}])
after = db.children(UID, db.tracks(UID)[0].id)
regressed = db.find_nodes(UID, "P1 Linear")[0]
check("add_plan on a same-named track really does replace it",
      len(after) == 1 and regressed.progress == 0,
      f"{len(after)} phases, progress={regressed.progress}")

print("\n5. Rebuild, then remove ONE phase only")
tools.add_plan(UID, [{"title": "DSA", "kind": "track", "children": [
    {"title": "P1 Linear", "kind": "phase", "target": 45},
    {"title": "P2 Search", "kind": "phase", "target": 25},
    {"title": "P3 Stack", "kind": "phase", "target": 30}]}])
print(tools.remove_plan_item(UID, title="P2 Search"))
left = [k.title for k in db.children(UID, db.tracks(UID)[0].id)]
check("only P2 removed", left == ["P1 Linear", "P3 Stack"], str(left))

print("\n6. Complete then reopen (undo a mistake)")
tools.log_progress(UID, title="P1 Linear", count=45)
p1 = db.find_nodes(UID, "P1 Linear")[0]
check("auto-completed at target", p1.status == "done", p1.status)
print(tools.reopen_item(UID, title="P1 Linear"))
p1 = db.find_nodes(UID, "P1 Linear")[0]
check("reopened", p1.status == "open", p1.status)
check("count kept on reopen", p1.progress == 45, str(p1.progress))

print("\n7. Correct a count outright, and edit a DONE item (old code couldn't find it)")
print(tools.edit_plan_item(UID, title="P1 Linear", progress=12, status="done"))
print(tools.edit_plan_item(UID, title="P1 Linear", gate="edited while done"))
p1 = db.find_nodes(UID, "P1 Linear")[0]
check("progress set outright", p1.progress == 12, str(p1.progress))
check("a done item is still editable", p1.gate == "edited while done", str(p1.gate))

print("\n8. Habits â€” add under a track, tick, then remove just that one")
tools.add_plan(UID, [{"title": "Life", "kind": "track", "children": [
    {"title": "Calisthenics", "kind": "habit", "recur": "4x_week"},
    {"title": "Philosophy", "kind": "habit", "recur": "daily"}]}])
print(tools.check_habit(UID, "Calisthenics"))
print(tools.remove_plan_item(UID, title="Philosophy"))
names = [h.title for h in db.habits(UID)]
check("only Philosophy gone", names == ["Calisthenics"], str(names))
check("streak intact", db.habits(UID)[0].streak == 1, str(db.habits(UID)[0].streak))

print("\n9. Ambiguity and misses are handled, not crashed")
amb = tools.edit_plan_item(UID, title="P", gate="x")
check("ambiguous match asks instead of guessing", "which" in amb.lower(), amb[:60])
miss = tools.edit_plan_item(UID, title="zzzznope", gate="x")
check("no match says so", "matches" in miss.lower(), miss[:60])
check("add_to_plan with bad parent is safe", "matches" in tools.add_to_plan(
    UID, "zzzznope", [{"title": "x"}]).lower())

print("\n10. Existing features untouched")
check("log_transaction works", "450" in tools.log_transaction(UID, 450, "out", "food"))
check("get_summary works", "450" in tools.get_summary(UID))
check("show_plan works", "DSA" in tools.show_plan(UID))
check("what_now works", len(tools.what_now(UID)) > 10)
snap = tools.plan_snapshot(UID)
check("plan_snapshot still renders", "DSA" in snap and "Life" in snap, snap[:80])
check("every registered tool is callable",
      all(callable(f) for f in tools.TOOLS.values()))
names_t = {s["function"]["name"] for s in tools.SCHEMAS}
check("schemas and registry agree", names_t == set(tools.TOOLS),
      str(names_t ^ set(tools.TOOLS)))

print("\n" + ("ALL PASSED" if not fails else f"{len(fails)} FAILED: {fails}"))
sys.exit(1 if fails else 0)

