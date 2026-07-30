"""The semantic path itself, with a stubbed embeddings endpoint.

Toy 4-dim vectors stand in for the real model: [dsa, money, infra, health].
That's enough to prove ranking, storage, thresholds and the recall merge â€”
the parts that are our code rather than OpenAI's.
"""
import os, sys, tempfile

tmp = os.path.join(tempfile.mkdtemp(), "t.db")
os.environ["DATABASE_URL"] = f"sqlite:///{tmp}"
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "x")
os.environ.setdefault("OPENAI_API_KEY", "x")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from cryptography.fernet import Fernet
os.environ["ENCRYPTION_KEY"] = Fernet.generate_key().decode()

import db
from brain import memory, tools

db.init_db()
UID = 444
db.get_or_create_user(UID, "Test")
fails = []


def check(label, cond, detail=""):
    print(("  ok  " if cond else "  FAIL") + f"  {label}" + (f"  <- {detail}" if not cond and detail else ""))
    if not cond:
        fails.append(label)


TOPICS = {
    "dsa": [1, 0, 0, 0], "leetcode": [.9, 0, .1, 0], "problems": [.85, 0, 0, 0],
    "money": [0, 1, 0, 0], "paid": [0, .9, 0, 0], "rupees": [0, .95, 0, 0],
    "redis": [0, 0, 1, 0], "cache": [0, 0, .95, 0], "caching": [0, 0, .97, 0],
    "latency": [0, 0, .8, 0], "p95": [0, 0, .85, 0],
    "gym": [0, 0, 0, 1], "calisthenics": [0, 0, 0, .95], "pushups": [0, 0, 0, .9],
}
calls = {"n": 0}


def fake_embed(text):
    """Sum the topic vectors of any known words in the text, then normalise."""
    calls["n"] += 1
    vec = [0.0, 0.0, 0.0, 0.0]
    for word in text.lower().replace(",", " ").replace(".", " ").split():
        if word in TOPICS:
            for i, v in enumerate(TOPICS[word]):
                vec[i] += v
    return vec if any(vec) else [0.01, 0.01, 0.01, 0.01]


memory._embed = fake_embed
memory._available = True

print("\n1. Store history across four topics")
HISTORY = [
    "I decided to use redis cache aside on three endpoints to cut latency",
    "solved 12 leetcode problems on two pointers today",
    "paid 2400 rupees for electricity this month",
    "did calisthenics pushups four times this week",
    "p95 dropped from 340ms to 110ms after the cache went in",
]
for h in HISTORY:
    memory.remember(UID, h)
check("all five embedded", db.count_memories(UID) == 5, str(db.count_memories(UID)))

print("\n2. Recall by MEANING â€” no shared keyword with the stored text")
hits = memory.search(UID, "caching", limit=3)
print(f"   query 'caching' -> {[h['text'][:45] for h in hits]}")
# Both infra lines are the same direction in this toy space, so assert on the
# SET returned, not the order — ordering between ties proves nothing here.
check("finds the redis decision, which shares no keyword with 'caching'",
      any("redis cache aside" in h["text"] for h in hits),
      str([h["text"][:40] for h in hits]))
check("the p95 line ranks too (same topic)",
      any("p95" in h["text"] for h in hits), str([h["text"][:30] for h in hits]))
check("unrelated money line is NOT returned",
      not any("rupees" in h["text"] for h in hits))

print("\n3. Each topic retrieves its own")
for query, expect in [("dsa", "leetcode"), ("money", "rupees"), ("gym", "calisthenics")]:
    hits = memory.search(UID, query, limit=2)
    got = hits[0]["text"] if hits else ""
    check(f"'{query}' -> {expect}", expect in got, got[:60])

print("\n4. Ranking is by score, best first")
hits = memory.search(UID, "redis cache", limit=5)
scores = [h["score"] for h in hits]
check("scores descending", scores == sorted(scores, reverse=True), str(scores))
check("threshold filters the weak matches", all(s >= 0.25 for s in scores), str(scores))

print("\n5. Nothing is stored twice, and noise is skipped")
before = db.count_memories(UID)
memory.remember(UID, HISTORY[0])
check("duplicate not re-embedded", db.count_memories(UID) == before)
check("short filler skipped", memory.remember(UID, "ok thanks") is False)
check("still 5 stored", db.count_memories(UID) == 5)

print("\n6. recall() merges semantic + keyword, without duplicating")
db.save_turn(UID, "user", "the exact figure was 2400 rupees for electricity")
out = tools.recall(UID, "caching")
print("   " + out.replace("\n", "\n   ")[:300])
check("semantic hit present", "redis cache aside" in out)
check("no 'keyword only' disclaimer when semantic is live",
      "keyword search only" not in out)
lines = [l for l in out.splitlines() if "redis cache aside" in l]
check("the same memory appears once, not twice", len(lines) == 1, str(len(lines)))

print("\n7. Isolation â€” another user's memories are invisible")
OTHER = 445
db.get_or_create_user(OTHER, "Other")
memory.remember(OTHER, "my own redis cache secret plan for something else")
mine = memory.search(UID, "redis", limit=10)
check("no cross-user leakage", all("secret" not in h["text"] for h in mine))
check("their own memory is findable by them",
      any("secret" in h["text"] for h in memory.search(OTHER, "redis", limit=5)))

print("\n8. Backfill embeds pre-existing history")
db.save_turn(UID, "user", "earlier I said the p95 target for checkout is 200ms")
db.save_turn(UID, "user", HISTORY[0])          # already embedded -> must skip
db.save_turn(UID, "user", "yes")               # too short -> must skip
before = db.count_memories(UID)
done, skipped = memory.backfill(UID, limit=50)
check("backfill embedded the new history", done >= 1, f"done={done} skipped={skipped}")
check("backfill skips duplicates and noise", skipped >= 2, f"skipped={skipped}")
check("count grew by exactly what it embedded",
      db.count_memories(UID) == before + done,
      f"{before} + {done} != {db.count_memories(UID)}")
check("the newly backfilled line is now searchable",
      any("checkout" in h["text"] for h in memory.search(UID, "p95 latency", limit=5)))

print("\n" + ("ALL PASSED" if not fails else f"{len(fails)} FAILED: {fails}"))
sys.exit(1 if fails else 0)
