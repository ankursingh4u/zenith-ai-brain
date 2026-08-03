"""Web access + SSRF blocking + fuzzy matching."""
import os, sys, tempfile

tmp = os.path.join(tempfile.mkdtemp(), "t.db")
os.environ["DATABASE_URL"] = f"sqlite:///{tmp}"
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "x")
os.environ.setdefault("OPENAI_API_KEY", "x")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from cryptography.fernet import Fernet
os.environ["ENCRYPTION_KEY"] = Fernet.generate_key().decode()

import db
from brain import tools, web

db.init_db()
UID = 333
db.get_or_create_user(UID, "Test")
fails = []


def check(label, cond, detail=""):
    print(("  ok  " if cond else "  FAIL") + f"  {label}" + (f"  <- {detail}" if not cond and detail else ""))
    if not cond:
        fails.append(label)


print("\n1. SSRF: the server's own network must be unreachable")
BLOCKED = [
    ("http://169.254.169.254/latest/meta-data/", "cloud metadata / IAM creds"),
    ("http://localhost:8000/admin", "localhost"),
    ("http://127.0.0.1/", "loopback"),
    ("http://192.168.1.1/", "private LAN"),
    ("http://10.0.0.5:5432/", "private LAN"),
    ("http://[::1]/", "ipv6 loopback"),
    ("file:///etc/passwd", "file scheme"),
    ("ftp://example.com/x", "ftp scheme"),
    ("gopher://evil/", "gopher scheme"),
]
for url, why in BLOCKED:
    ok, reason = web.check_url(url)
    check(f"blocked: {why}", ok is False, f"{url} -> allowed!")
    out = tools.read_page(UID, url)
    check(f"  tool refuses it too ({why})", "Couldn't read that page" in out, out[:70])

print("\n2. Public URLs are allowed")
for url in ["https://aws.amazon.com/lambda/pricing/", "https://example.com"]:
    ok, reason = web.check_url(url)
    check(f"allowed: {url}", ok is True, reason)

print("\n3. Reading a real page returns readable text")
out = tools.read_page(UID, "https://example.com", 2000)
print("   " + out.replace("\n", " ")[:150])
check("page read succeeded", "Couldn't read" not in out, out[:90])
check("returns actual prose, not html", "<html" not in out.lower(), out[:90])
check("names the source url", "example.com" in out)

print("\n4. Search tool formats results with URLs")
res = tools.web_search(UID, "postgres 17 release notes", 3)
print("   " + res.replace("\n", " ")[:150])
check("search returned results", "No results" not in res and "Couldn't search" not in res)
check("includes clickable urls", "http" in res)
check("tells the model to read before quoting", "read_page" in res)

print("\n5. Oversized / non-page content is refused politely")
out = tools.read_page(UID, "https://example.com/definitely-not-here-404")
check("404 handled without crashing", isinstance(out, str) and len(out) > 10, out[:60])

print("\n6. Fuzzy matching finds items despite typos and rewording")
tools.add_plan(UID, [{"title": "DSA", "kind": "track", "children": [
    {"title": "P1 Linear Arrays", "kind": "phase", "target": 45, "gate": "pattern in 60s"},
    {"title": "P3 Stack and Monotonic Stack", "kind": "phase", "target": 30,
     "gate": "histogram cold"},
    {"title": "P7 Dynamic Programming", "kind": "phase", "target": 45}]}])
tools.add_to_plan(UID, "DSA", [{"title": "Calisthenics", "kind": "habit", "recur": "4x_week"}])

for typed, expect in [
    ("P3 stak", "P3 Stack"),
    ("monotonic", "P3 Stack"),
    ("dynamic programing", "P7 Dynamic"),
    ("P1 linear", "P1 Linear"),
    ("arrays", "P1 Linear"),
]:
    node, problem = tools._pick_node(UID, None, typed)
    got = node.title if node else f"(no match: {problem[:40]})"
    check(f"'{typed}' -> {expect}", node is not None and expect in got, got)

print("\n7. Fuzzy habit ticking")
out = tools.check_habit(UID, "callisthenics")     # misspelled
check("misspelled habit still ticks", "Streak" in out, out[:60])

print("\n8. Fuzzy never overrides an exact match")
node, _ = tools._pick_node(UID, None, "P7 Dynamic Programming")
check("exact title wins", node is not None and node.title == "P7 Dynamic Programming",
      node.title if node else "none")

print("\n9. Nonsense still fails cleanly rather than matching something random")
node, problem = tools._pick_node(UID, None, "zzzqqqxyw")
check("no false match on nonsense", node is None, node.title if node else "")
check("says so plainly", "Nothing in your plan matches" in (problem or ""), problem or "")

print("\n9b. YouTube links are recognised in every shape they come in")
for url, want in (
    ("https://www.youtube.com/watch?v=dQw4w9WgXcQ", "dQw4w9WgXcQ"),
    ("https://youtu.be/dQw4w9WgXcQ", "dQw4w9WgXcQ"),
    ("https://youtu.be/dQw4w9WgXcQ?t=42", "dQw4w9WgXcQ"),
    ("https://www.youtube.com/watch?feature=share&v=dQw4w9WgXcQ&t=1", "dQw4w9WgXcQ"),
    ("https://m.youtube.com/watch?v=dQw4w9WgXcQ", "dQw4w9WgXcQ"),
    ("https://www.youtube.com/shorts/dQw4w9WgXcQ", "dQw4w9WgXcQ"),
    ("https://www.youtube.com/embed/dQw4w9WgXcQ", "dQw4w9WgXcQ"),
    ("dQw4w9WgXcQ", "dQw4w9WgXcQ"),
    ("https://vimeo.com/12345", None),
    ("https://example.com/watch?v=dQw4w9WgXcQ", None),
    ("not a url", None),
):
    check(f"{url[:46]:<46} -> {want}", web.youtube_id(url) == want, str(web.youtube_id(url)))

print("\n9c. A video is actually read (live network)")
out = tools.read_video(UID, "https://www.youtube.com/watch?v=dQw4w9WgXcQ")
check("transcript came back, not a page footer", len(out) > 500, f"{len(out)} chars")
check("it is the spoken words", "never gonna give you up" in out.lower(), out[:200])
check("titled from oEmbed", "🎬" in out and "Rick" in out, out[:120])
check("says which language", "Transcript language" in out)
check("a non-YouTube link is refused clearly",
      "isn't a YouTube link" in tools.read_video(UID, "https://example.com"))
check("nonsense doesn't crash it", "Couldn't read that video" in tools.read_video(UID, "banana"))

print("\n10. Nothing regressed")
check("registry and schemas agree",
      {s["function"]["name"] for s in tools.SCHEMAS} == set(tools.TOOLS))
check("web tools registered", "web_search" in tools.TOOLS and "read_page" in tools.TOOLS)
check("show_plan works", "DSA" in tools.show_plan(UID))
check("money works", "450" in tools.log_transaction(UID, 450, "out", "food"))
check("plan_gaps works", len(tools.plan_gaps(UID)) > 10)

print("\n" + ("ALL PASSED" if not fails else f"{len(fails)} FAILED: {fails}"))
sys.exit(1 if fails else 0)
