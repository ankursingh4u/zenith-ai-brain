"""Load one person's profile, tracks and habits straight into the database.

Why a script and not just pasting it to the bot: a 28-phase roadmap sent through
the model is a roadmap the model can quietly reword, merge or drop a row from.
Here every gate, count and rule lands exactly as written, and re-running it is
safe — the two tracks are replaced, habits already tracked are left alone, and
nothing else in the database is touched.

    python scripts/setup_me.py                 # dry run: shows what it would do
    python scripts/setup_me.py --apply
    python scripts/setup_me.py --apply --telegram-id 1786765907
    python scripts/setup_me.py --apply --notes  # also seed the Obsidian vault

Point it at whichever database you mean with DATABASE_URL — the local brain.db
by default, or the server's /data/brain.db when run inside the container.
"""
from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config          # noqa: E402
import db              # noqa: E402
from brain import tools  # noqa: E402

# --- Who this person is ---------------------------------------------------
# Their own words. This is injected into every turn, and the agent is told the
# profile outranks its own defaults — so the handling rules belong here too.
PROFILE = """Ankur. Software dev — company job plus my own products.
Know: HTML, CSS, JS, Node, JWT, APIs, async/await. Backends so far were one file, no layers.
Redis, Lambda, DynamoDB are just names to me. No DSA yet.
Goal: elite engineer. Build anything without an LLM holding my hand. Learn from books and source,
understand the code, design systems for millions, really know backend, be the one others come to
when stuck. Not dependent on Claude. Not employable — undeniable.
HOW TO HANDLE ME:
- Never label things P1/P2/P3. Name the topic.
- No week or month deadlines. Just the order and a clear "done when".
- One topic at a time. Nothing new until the current one is done.
- Off-topic research urge: send it to the inbox for Sunday, and name what I am on.
- If I say I have to do something, make it a dated commitment and chase me. Not what I parked.
- Progress = topics finished, never hours logged.
- Be blunt when I stall.
- My life is company work, my own projects, calisthenics, philosophy books, her, and movies —
  never suggest cutting any of it.
MY AI RULE (hold me to it): zero AI and zero autocomplete for all DSA and the first three Dev
topics. After that, only if I can answer "why this way, what breaks at 10x?" If I can't, I delete
it and write it myself. Stuck = book, docs or source first, never a prompt. Call me out when I
lean on AI — do not hand me the solution inside that zone even if I ask directly."""

DSA_RULES = ("JS on LeetCode. NeetCode 150, then Blind 75, then Striver. "
             "Stuck 20 min: read the editorial, close it, rewrite from memory. "
             "Re-solve at day 7 and day 30 or it does not count. ~290 problems then stop. "
             "Zero AI, zero autocomplete, blank editor.")

DEV_RULES = ("ONE repo forever: a multi-tenant task/project SaaS. Every topic upgrades that same "
             "app. Node, TypeScript, Fastify, Postgres, Prisma, Redis, Docker, AWS. "
             "k6 after each topic into BENCHMARKS.md — no numbers means unfinished. "
             "Start each topic by breaking the last version.")

# (title, count, done-when). Order is the plan; there are no dates by design.
DSA_PHASES = [
    ("Arrays and Two Pointers", 25, "Name the pattern in a minute"),
    ("Sliding Window", 12, "Fixed vs variable is instant"),
    ("Hashing and Prefix Sum", 12, "Hashmap before nested loop"),
    ("Binary Search incl on answer", 25, "Bounds right, no off-by-one, 5 cold"),
    ("Stack and Monotonic Stack", 18, "Largest Rectangle cold"),
    ("Linked List", 12, "Reversal and cycles are muscle memory"),
    ("Trees and BST", 22, "Any recursive tree solution made iterative"),
    ("Heap and Top-K", 13, "Spot a top-K problem unprompted"),
    ("Backtracking", 20, "Template from memory, blank editor"),
    ("Graphs: BFS, DFS, Topo, Union-Find, Dijkstra", 35, "Model a real problem as a graph"),
    ("DP: 1D, grid, knapsack, LIS, subsequences, trees", 45, "Recurrence on paper before code"),
    ("Tries, Intervals, Greedy, Bits", 25, "Any of them in 25 min"),
    ("Contests and mocks", 30, "3 contests at 3 of 4, 5 mocks in 45 min"),
]

DEV_PHASES = [
    ("Layered code", None, "CRUD feature in 3 files, under 30 min, no AI"),
    ("Real database, SQL then Prisma", None, "Seq scan to index scan, proven with EXPLAIN"),
    ("Deploy by hand then CI", None, "A push is live in under 5 min"),
    ("IAM, S3, Route 53, HTTPS", None, "Least-privilege policy written by hand, no wizard"),
    ("Redis caching", None, "p95 drops 60% on a cached route, k6 proven"),
    ("Background work: queues, S3, Lambda, SQS, DLQ", None,
     "POST under 100ms, kill the worker, lose nothing"),
    ("Resilience: logs, alarms, timeouts, breaker, secrets", None,
     "Stop Postgres under load, nothing crashes"),
    ("Tests", None, "Refactor the service layer, the suite catches every break"),
    ("Containers and Fargate", None, "A push gives a zero-downtime rollout"),
    ("Scale out", None, "3 instances, 3x throughput, p99 flat"),
    ("Scale data: pooling, replica, partitioning", None, "10M rows, p99 unmoved"),
    ("Events and DynamoDB single-table", None, "5 access patterns, zero scans"),
    ("Observability", None, "Find an injected slow query from the dashboard in 3 min"),
    ("Terraform", None, "Destroy then apply rebuilds it all, no console clicks"),
    ("System design, 20 systems", 20, "Any one in 45 min with trade-offs and numbers"),
]

# (title, recur, notes)
HABITS = [
    ("DSA", "daily", "1 hour, blank editor, phone in another room"),
    ("Dev", "daily", "2 hours, current topic only"),
    ("Calisthenics", "4x_week", "45 min, never traded for code"),
    ("Read a book", "daily", "30 min, technical or philosophy, on paper"),
    ("Talk to her", "daily", "Non-negotiable"),
    ("Research inbox", "daily", "Dump the urge in 10 seconds, read it Sunday"),
]


def life_track(uid: int) -> int:
    """The track habits hang under. Without it add_habit leaves each habit at
    the root, where show_plan renders it as if it were a track of its own."""
    for t in db.tracks(uid):
        if (t.title or "").strip().lower() in ("life", "habits"):
            return t.id
    return db.add_node(uid, "Life", "track", None, "Life",
                       "The things that don't move: training, reading, her, the inbox.",
                       None, 2, None, None, 99)


def tidy_habits(uid: int, life_id: int) -> int:
    """Pull any root-level habits under Life, keeping their streaks."""
    moved = 0
    for h in db.habits(uid):
        if h.parent_id is None and h.id != life_id:
            if db.reparent_node(uid, h.id, life_id, "Life"):
                moved += 1
    return moved


def _phases(rows):
    out = []
    for title, target, gate in rows:
        item = {"title": title, "kind": "phase", "gate": gate}
        if target:
            item["target"] = target
        out.append(item)
    return out


def plan_payload() -> list[dict]:
    return [
        {"title": "DSA", "kind": "track", "notes": DSA_RULES,
         "children": _phases(DSA_PHASES)},
        {"title": "Dev", "kind": "track", "notes": DEV_RULES,
         "children": _phases(DEV_PHASES)},
    ]


def seed_notes(uid: int) -> list[str]:
    """A starting vault: the rules, and what 'now' means. Needs a linked vault."""
    from brain import notes
    out = []
    first_dsa, first_dev = DSA_PHASES[0][0], DEV_PHASES[0][0]
    out.append(tools.write_note(
        uid, "Now", (
            f"One topic at a time. Nothing new until the current one is done.\n\n"
            f"- DSA: **{first_dsa}** — done when: {DSA_PHASES[0][2]} ({DSA_PHASES[0][1]} problems)\n"
            f"- Dev: **{first_dev}** — done when: {DEV_PHASES[0][2]}\n\n"
            f"Rules: [[DSA Rules]] · [[Dev Rules]]\n\n"
            f"#now"), folder=None))
    out.append(tools.write_note(uid, "DSA Rules", DSA_RULES + "\n\n#dsa", folder="DSA"))
    out.append(tools.write_note(uid, "Dev Rules", DEV_RULES + "\n\n#dev", folder="Dev"))
    _ = notes  # imported to fail loudly here rather than inside a tool call
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="Seed a user's profile, tracks and habits.")
    ap.add_argument("--telegram-id", type=int, default=None)
    ap.add_argument("--apply", action="store_true", help="Actually write. Without it, dry run.")
    ap.add_argument("--replace-profile", action="store_true",
                    help="Overwrite the existing profile instead of leaving it alone.")
    ap.add_argument("--notes", action="store_true",
                    help="Also write the starter notes into the linked Obsidian vault.")
    args = ap.parse_args()

    uid = args.telegram_id
    if uid is None:
        if not config.ALLOWED_TELEGRAM_IDS:
            print("No --telegram-id given and ALLOWED_TELEGRAM_IDS is empty.")
            return 2
        uid = sorted(config.ALLOWED_TELEGRAM_IDS)[0]

    db.init_db()
    print(f"Database : {config.DATABASE_URL}")
    print(f"User     : {uid}")
    existing_tracks = [t.title for t in db.tracks(uid)]
    print(f"Tracks now: {existing_tracks or 'none'}")
    print(f"Habits now: {[h.title for h in db.habits(uid)] or 'none'}")
    print(f"Profile  : {'set (' + str(len(db.get_profile(uid) or '')) + ' chars)' if db.get_profile(uid) else 'empty'}")
    print(f"\nWould store: 2 tracks (DSA {len(DSA_PHASES)} topics, Dev {len(DEV_PHASES)} topics), "
          f"{len(HABITS)} habits, {len(PROFILE.splitlines())} profile lines.")

    if not args.apply:
        print("\nDry run — nothing written. Re-run with --apply.")
        return 0

    db.get_or_create_user(uid, "Ankur")

    current = db.get_profile(uid)
    if current and not args.replace_profile:
        merged = current.rstrip() + "\n" + PROFILE
        db.set_profile(uid, merged)
        print("\nProfile: appended (kept what was there — use --replace-profile to overwrite).")
    else:
        db.set_profile(uid, PROFILE)
        print("\nProfile: set.")

    # add_plan replaces a same-named track, so re-running updates DSA/Dev and
    # leaves any other track (and its recorded progress) alone.
    print(tools.add_plan(uid, plan_payload()))

    # Make the Life track first so habits nest under it instead of pretending
    # to be tracks, then adopt any that were added before it existed.
    life_id = life_track(uid)
    for title, recur, note in HABITS:
        print(" ", tools.add_habit(uid, title, recur, note))
    moved = tidy_habits(uid, life_id)
    if moved:
        print(f"  Moved {moved} habit(s) under Life (streaks kept).")

    if args.notes:
        print("\nSeeding the vault:")
        for line in seed_notes(uid):
            print(" ", line.replace("\n", " | "))

    print("\nDone. In Telegram: 'show plan', 'what now?', 'habits'.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
