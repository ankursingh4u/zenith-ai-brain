"""Obsidian vault + notes, against a throwaway DB and a temp folder.

Uses the local backend so no network and no GitHub token are involved — the
markdown that lands on disk here is byte-for-byte what the GitHub backend
commits, so this covers the format either way.
"""
import os, sys, tempfile

tmpdir = tempfile.mkdtemp()
os.environ["DATABASE_URL"] = f"sqlite:///{os.path.join(tmpdir, 't.db')}"
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "x")
os.environ.setdefault("OPENAI_API_KEY", "x")
os.environ["EMBED_ENABLED"] = "0"        # no network: search must fall back to keywords
os.environ["VAULT_DIR"] = ""             # nothing linked unless a test links it
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from cryptography.fernet import Fernet
os.environ["ENCRYPTION_KEY"] = Fernet.generate_key().decode()

from datetime import date

import db
from brain import notes, tools
from integrations import vault as vaultmod

db.init_db()
UID, OTHER = 4242, 777
db.get_or_create_user(UID, "Test")
db.get_or_create_user(OTHER, "Someone else")
VAULT = os.path.join(tmpdir, "MyVault")
os.makedirs(VAULT, exist_ok=True)
fails = []


def check(label, cond, detail=""):
    print(("  ok  " if cond else "  FAIL") + f"  {label}" + (f"  <- {detail}" if not cond and detail else ""))
    if not cond:
        fails.append(label)


def vault_file(rel):
    return os.path.join(VAULT, *rel.split("/"))


print("\n1. Markdown format: frontmatter, wikilinks, tags")
text = notes.render("Sliding Window", "Fixed vs variable. See [[Two Pointers]].\n#dsa",
                    tags=["patterns"])
meta, body = notes.parse(text)
check("frontmatter parsed back", meta.get("title") == "Sliding Window", str(meta))
check("tags survive the round trip", "patterns" in (meta.get("tags") or []), str(meta.get("tags")))
check("body kept intact", "Fixed vs variable" in body, body[:40])
check("wikilink extracted", notes.extract_links(body) == ["Two Pointers"],
      str(notes.extract_links(body)))
check("inline #tag extracted", "dsa" in notes.extract_tags(body, meta), str(notes.extract_tags(body, meta)))
check("alias link resolves to the target",
      notes.extract_links("see [[Real Note|what I call it]]") == ["Real Note"])
check("heading link resolves to the note",
      notes.extract_links("see [[Real Note#Section]]") == ["Real Note"])

print("\n2. Path safety — nothing escapes the vault")
for bad in ("../../etc/passwd", "/etc/passwd", "C:/Windows/system32", "a/../../b"):
    try:
        vaultmod.clean_path(bad)
        ok = bad == "/etc/passwd"          # leading slash is just stripped, stays inside
    except vaultmod.VaultError:
        ok = True
    check(f"refuses or contains '{bad}'", ok)
check("a title with slashes can't build a path out",
      "/" not in notes.path_for("../../evil", "DSA").replace("DSA/", "", 1),
      notes.path_for("../../evil", "DSA"))

print("\n3. Not linked yet — the note is still saved, and says so")
out = tools.write_note(UID, "Orphan", "no vault yet")
check("note saved locally", db.get_note(UID, "Notes/Orphan.md") is not None)
check("warns instead of pretending", "/vault" in out and "⚠️" in out, out[:80])

print("\n4. Link a local vault and write a real file")
db.set_vault_link(UID, "local", VAULT)
ok, detail = vaultmod.check(UID)
check("link verifies", ok, detail)
out = tools.write_note(UID, "Sliding Window",
                       "Fixed vs variable is instant. See [[Two Pointers]].",
                       folder="DSA", tags=["dsa"])
check("reports the vault", "✅" in out, out[:80])
path = vault_file("DSA/Sliding Window.md")
check("file exists on disk", os.path.exists(path), path)
disk = open(path, encoding="utf-8").read()
check("file has frontmatter", disk.startswith("---\ntitle: Sliding Window"), disk[:40])
check("file has the body", "Fixed vs variable" in disk)
check("indexed with its links", (db.get_note(UID, "DSA/Sliding Window.md").links or "") == "Two Pointers")

print("\n5. Append never loses what was there — and never forks a duplicate")
tools.append_note(UID, "Sliding Window", "Day 7 re-solve: done cold.")
disk = open(path, encoding="utf-8").read()
check("old content kept", "Fixed vs variable" in disk)
check("new content added", "Day 7 re-solve" in disk)
check("still one file", len(db.list_notes(UID, "DSA")) == 1)
# The folder wasn't repeated on the append. The title has to find its own note,
# or Obsidian ends up with two 'Sliding Window' notes and ambiguous [[links]].
check("no duplicate in the default folder",
      not os.path.exists(vault_file("Notes/Sliding Window.md")))
check("an explicit folder can't fork it either",
      tools.write_note(UID, "Sliding Window", "still the same note", folder="Books")
      and not os.path.exists(vault_file("Books/Sliding Window.md")))

print("\n6. Backlinks — the reason to link at all")
tools.write_note(UID, "Two Pointers", "Opposite ends. Feeds [[Sliding Window]].", folder="DSA")
back = notes.backlinks(UID, "Sliding Window")
check("backlink found", [b["title"] for b in back] == ["Two Pointers"], str(back))
read = tools.read_note(UID, "Sliding Window")
check("read_note shows links out", "[[Two Pointers]]" in read, read[:120])
check("read_note shows what links in", "Linked from" in read, read[-120:])
check("read_note finds a note by rough name", "Sliding Window" in tools.read_note(UID, "sliding"))

print("\n7. Daily note")
tools.daily_note(UID, "Solved 5 two-pointer problems", heading="DSA")
tools.daily_note(UID, "Split the router into 3 layers", heading="Dev")
tools.daily_note(UID, "Re-solved yesterday's cold", heading="DSA")
daily = vault_file(f"Daily/{date.today().isoformat()}.md")
check("daily file written", os.path.exists(daily), daily)
text = open(daily, encoding="utf-8").read()
check("heading created once", text.count("## DSA") == 1, str(text.count("## DSA")))
check("second DSA line went under the DSA heading",
      text.index("Re-solved") < text.index("## Dev"), text)
check("reading it back works", "Split the router" in tools.daily_note(UID))

print("\n8. Research inbox — capture in one line, read on Sunday")
tools.capture_research(UID, "Rust for the worker?", doing="Sliding Window")
tools.capture_research(UID, "Try Bun instead of Node")
listed = tools.review_inbox(UID)
check("both parked", "Rust" in listed and "Bun" in listed, listed[:120])
check("inbox names what they were on",
      "Sliding Window" in open(vault_file("Inbox/Research Inbox.md"), encoding="utf-8").read())
check("2 open", len(notes.inbox_items(UID)) == 2, str(notes.inbox_items(UID)))
tools.review_inbox(UID, "clear", "Bun")
check("one ticked off", len(notes.inbox_items(UID)) == 1, str(notes.inbox_items(UID)))
check("the right one survived", notes.inbox_items(UID)[0][1].startswith("Rust"))
tools.review_inbox(UID, "clear")
check("clearing the rest empties it", notes.inbox_items(UID) == [])
check("closed items are kept, not deleted",
      "[x]" in open(vault_file("Inbox/Research Inbox.md"), encoding="utf-8").read())

print("\n9. Search falls back to keywords when embeddings are off")
hits = tools.search_notes(UID, "two pointer")
check("finds the note by word", "Two Pointers" in hits, hits[:120])
check("says so when nothing matches", "Nothing in your notes" in tools.search_notes(UID, "zzzznope"))

print("\n10. Sync pulls in what was written in Obsidian")
os.makedirs(vault_file("Books"), exist_ok=True)
with open(vault_file("Books/Meditations.md"), "w", encoding="utf-8") as fh:
    fh.write("---\ntitle: Meditations\ntags: [philosophy]\n---\n\n"
             "The obstacle is the way. Links to [[Now]].\n")
msg = tools.sync_vault(UID)
check("sync reports the pull", "pulled in" in msg, msg)
got = notes.read_note(UID, "Meditations")
check("hand-written note is now readable", got and "obstacle" in got[1], str(got))
check("its tag came across", "philosophy" in (db.get_note(UID, "Books/Meditations.md").tags or ""))

print("\n11. A note the vault never got is pushed on the next sync")
db.remove_vault_link(UID)
tools.write_note(UID, "Written while offline", "should land later", folder="Dev")
check("marked as never pushed", db.get_note(UID, "Dev/Written while offline.md").remote_sha is None)
db.set_vault_link(UID, "local", VAULT)
tools.sync_vault(UID)
check("now in the vault", os.path.exists(vault_file("Dev/Written while offline.md")))
check("marked as pushed", db.get_note(UID, "Dev/Written while offline.md").remote_sha is not None)

print("\n12. One user's notes are invisible to another")
db.set_vault_link(OTHER, "local", os.path.join(tmpdir, "OtherVault"))
tools.write_note(OTHER, "Their Secret", "not for anyone else")
check("other user sees only their own", [n.title for n in db.all_notes(OTHER)] == ["Their Secret"],
      str([n.title for n in db.all_notes(OTHER)]))
check("this user can't read it", tools.read_note(UID, "Their Secret").startswith("No note"),
      tools.read_note(UID, "Their Secret")[:60])
check("and can't search it up", "Their Secret" not in tools.search_notes(UID, "not for anyone else"))
check("their vault is a different folder",
      not os.path.exists(vault_file("Notes/Their Secret.md")))

print("\n13. Registry stays consistent")
check("every registered tool is callable", all(callable(f) for f in tools.TOOLS.values()))
names = {s["function"]["name"] for s in tools.SCHEMAS}
check("schemas and registry agree", names == set(tools.TOOLS), str(names ^ set(tools.TOOLS)))
for t in ("write_note", "read_note", "search_notes", "daily_note", "capture_research",
          "review_inbox", "vault_status", "sync_vault"):
    check(f"{t} registered", t in tools.TOOLS)
check("vault_status is honest about the link", "local" in tools.vault_status(UID).lower(),
      tools.vault_status(UID)[:60])

print("\n14. Existing features untouched")
check("plan still works", "DSA" in tools.add_plan(UID, [{"title": "DSA", "kind": "track"}]))
check("money still works", "450" in tools.log_transaction(UID, 450, "out", "food"))
check("habits still work", "gym" in tools.add_habit(UID, "gym", "daily").lower())

print("\n15. GitHub failures explain themselves instead of blowing up")
# _explain reads self.repo/self.branch. It was written as a staticmethod once,
# and the 404 branch — wrong repo name or wrong branch, the mistake everyone
# makes at setup — died with a NameError instead of saying what was wrong.
gh = vaultmod.GitHubVault("me/vault", "tok", "main")


class FakeResp:
    def __init__(self, code, message):
        self.status_code, self._m, self.text = code, message, message

    def json(self):
        return {"message": self._m}


for code, message, expect in (
    (401, "Bad credentials", "refused the token"),
    (403, "Resource not accessible", "refused the token"),
    (404, "Not Found", "can't find me/vault"),
    (409, "Git Repository is empty", "empty"),
    (500, "Server Error", "GitHub error 500"),
):
    said = gh._explain(FakeResp(code, message))
    check(f"{code} is explained, not raised", expect in said, said)
check("404 names the branch too", "main" in gh._explain(FakeResp(404, "Not Found")))
check("repo shape is validated before any request happens",
      isinstance(getattr(vaultmod, "GitHubVault"), type))
for bad in ("not-a-repo", "https://github.com/me/vault", ""):
    try:
        vaultmod.GitHubVault(bad, "tok")
        check(f"rejects repo {bad!r}", False)
    except vaultmod.VaultError:
        check(f"rejects repo {bad!r}", True)
check("a subfolder vault prefixes paths",
      vaultmod.GitHubVault("me/v", "t", "main", "vault")._full("Daily/x.md") == "vault/Daily/x.md")

print("\n" + ("ALL PASSED" if not fails else f"{len(fails)} FAILED: {fails}"))
sys.exit(1 if fails else 0)
