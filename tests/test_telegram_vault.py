"""Drive the real Telegram handlers with fake Update/Context objects.

The vault lives behind a command, and a command is code no other suite touches:
argument parsing, the access gate, deleting the message that carried a token,
and what actually gets sent back. This runs those paths for real — only the
network calls to GitHub are faked, so nothing here needs a token or a repo.
"""
import asyncio, os, sys, tempfile

tmpdir = tempfile.mkdtemp()
os.environ["DATABASE_URL"] = f"sqlite:///{os.path.join(tmpdir, 't.db')}"
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "x")
os.environ.setdefault("OPENAI_API_KEY", "x")
os.environ["EMBED_ENABLED"] = "0"
os.environ["VAULT_DIR"] = ""
os.environ["ALLOWED_TELEGRAM_IDS"] = "4242"
os.environ["GODFATHER_ANSWER"] = "open sesame"
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from cryptography.fernet import Fernet
os.environ["ENCRYPTION_KEY"] = Fernet.generate_key().decode()

import crypto
import db
from bot import telegram_bot as tb
from integrations import vault as vaultmod

db.init_db()
UID, STRANGER = 4242, 55
db.get_or_create_user(UID, "Ankur")
VAULT = os.path.join(tmpdir, "MyVault")
os.makedirs(VAULT, exist_ok=True)
fails = []


def check(label, cond, detail=""):
    print(("  ok  " if cond else "  FAIL") + f"  {label}" + (f"  <- {detail}" if not cond and detail else ""))
    if not cond:
        fails.append(label)


# --- The smallest fakes that behave like python-telegram-bot's objects ----
class FakeBot:
    def __init__(self):
        self.sent, self.deleted, self.actions = [], [], []

    async def send_message(self, chat_id, text, **kw):
        self.sent.append(text)

    async def delete_message(self, chat_id, message_id):
        self.deleted.append(message_id)

    async def send_chat_action(self, chat_id, action):
        self.actions.append(action)


class FakeMessage:
    def __init__(self, text, mid=1):
        self.text, self.message_id, self.replies = text, mid, []

    async def reply_text(self, text, **kw):
        self.replies.append(text)


class FakeUpdate:
    def __init__(self, uid, text, mid=1):
        self.effective_user = type("U", (), {"id": uid, "full_name": "Ankur"})()
        self.effective_chat = type("C", (), {"id": uid})()
        self.message = FakeMessage(text, mid)


class FakeCtx:
    def __init__(self, args):
        self.bot, self.args = FakeBot(), args


def run_vault(uid, args, text=None, mid=1):
    upd = FakeUpdate(uid, text or ("/vault " + " ".join(args)), mid)
    ctx = FakeCtx(args)
    asyncio.run(tb.vault_cmd(upd, ctx))
    return upd, ctx


print("\n1. /vault with no arguments explains both routes")
upd, ctx = run_vault(UID, [])
help_text = "\n".join(ctx.bot.sent)
check("something was sent", bool(ctx.bot.sent))
check("covers the github route", "/vault github" in help_text)
check("covers the local route", "/vault local" in help_text)
check("names the Obsidian plugin that does the pulling", "Obsidian Git" in help_text)
check("says the token is deleted and encrypted",
      "encrypted" in help_text and "delete" in help_text.lower())
check("fits in one Telegram message", all(len(m) <= 4096 for m in ctx.bot.sent),
      str([len(m) for m in ctx.bot.sent]))

print("\n2. /vault local links a real folder")
upd, ctx = run_vault(UID, ["local", VAULT])
check("confirmed", any("✅" in m for m in ctx.bot.sent), str(ctx.bot.sent))
link = db.get_vault_link(UID)
check("link saved", link is not None and link.kind == "local", str(link and link.kind))
check("points at the folder", link and link.repo == VAULT, str(link and link.repo))

print("\n3. A folder that doesn't exist is refused, not silently accepted")
upd, ctx = run_vault(UID, ["local", os.path.join(tmpdir, "does-not-exist")])
check("warned", any("⚠️" in m for m in ctx.bot.sent), str(ctx.bot.sent))
check("bad link not kept", db.get_vault_link(UID) is None)

print("\n4. /vault github — token handled like a password")
real_check = vaultmod.check
vaultmod.check = lambda uid: (True, "github me/vault — 3 markdown file(s) visible.")
try:
    upd, ctx = run_vault(UID, ["github", "me/vault", "ghp_secrettoken", "main"], mid=77)
finally:
    vaultmod.check = real_check
check("the message carrying the token was deleted", ctx.bot.deleted == [77], str(ctx.bot.deleted))
check("the token is never echoed back",
      not any("ghp_secrettoken" in m for m in ctx.bot.sent), str(ctx.bot.sent))
link = db.get_vault_link(UID)
check("link saved as github", link and link.kind == "github" and link.repo == "me/vault")
check("token stored encrypted, not in the clear",
      link and link.token_enc and "ghp_secrettoken" not in link.token_enc)
check("and decrypts back", crypto.decrypt(link.token_enc) == "ghp_secrettoken")
check("branch kept", link.branch == "main", str(link.branch))

print("\n5. A repo it can't reach is not saved as if it worked")
vaultmod.check = lambda uid: (False, "GitHub refused the token.")
try:
    upd, ctx = run_vault(UID, ["github", "me/wrong", "ghp_bad"], mid=78)
finally:
    vaultmod.check = real_check
check("told what went wrong", any("refused" in m for m in ctx.bot.sent), str(ctx.bot.sent))
check("nothing left linked", db.get_vault_link(UID) is None)

print("\n6. status, sync and unlink")
run_vault(UID, ["local", VAULT])
upd, ctx = run_vault(UID, ["status"])
check("status names the folder", any(VAULT in m for m in ctx.bot.sent), str(ctx.bot.sent))
upd, ctx = run_vault(UID, ["sync"])
check("sync reports back", any("Synced" in m or "sync" in m.lower() for m in ctx.bot.sent),
      str(ctx.bot.sent))
check("typing shown while it works", "typing" in ctx.bot.actions, str(ctx.bot.actions))
upd, ctx = run_vault(UID, ["unlink"])
check("unlinked", db.get_vault_link(UID) is None)
check("says the files are untouched", any("untouched" in m for m in ctx.bot.sent))
upd, ctx = run_vault(UID, ["nonsense"])
check("an unknown subcommand shows help", any("/vault github" in m for m in ctx.bot.sent))

print("\n7. A stranger can't link a vault through the gate")
upd = FakeUpdate(STRANGER, "/vault local " + VAULT, 9)
ctx = FakeCtx(["local", VAULT])
asyncio.run(tb.vault_cmd(upd, ctx))
check("asked for the access code instead", bool(upd.message.replies), str(upd.message.replies))
check("no vault linked for them", db.get_vault_link(STRANGER) is None)
check("nothing sent to them about vaults", not any("Obsidian" in m for m in ctx.bot.sent))

print("\n8. Long replies survive Telegram's 4096-char limit")
short = tb._chunks("just a line")
check("short text stays one message", short == ["just a line"], str(short))
long_reply = "\n\n".join(f"Paragraph {i}: " + "x" * 300 for i in range(40))
parts = tb._chunks(long_reply)
check("split into several", len(parts) > 1, str(len(parts)))
check("every part is sendable", all(len(p) <= 4096 for p in parts), str([len(p) for p in parts]))
check("nothing dropped", all(f"Paragraph {i}:" in "".join(parts) for i in range(40)))
check("split on paragraph breaks", all(not p.startswith("x") for p in parts))
one_line = "y" * 9000
parts = tb._chunks(one_line)
check("one enormous line is cut, not lost",
      len(parts) == 3 and sum(len(p) for p in parts) == 9000, str([len(p) for p in parts]))
sent = []


class Collector(FakeMessage):
    async def reply_text(self, text, **kw):
        sent.append(text)


asyncio.run(tb._reply(Collector(""), long_reply))
check("_reply sends every piece in order", len(sent) == len(tb._chunks(long_reply)))
check("first piece first", sent[0].startswith("Paragraph 0"), sent[0][:20])

print("\n9. The note tools reply in plain Telegram-safe text")
db.set_vault_link(UID, "local", VAULT)
from brain import tools
out = tools.write_note(UID, "Sliding Window", "Fixed vs variable. See [[Two Pointers]].", folder="DSA")
check("no markdown table pipes", "|" not in out, out)
check("no bold markers", "**" not in out, out)
check("says where it landed", "vault" in out.lower(), out)
check("short enough for one message", len(out) <= 4096, str(len(out)))
status_out = tools.vault_status(UID)
check("status is a few short lines", status_out.count("\n") <= 4 and len(status_out) < 500,
      status_out)

print("\n10. The bottom menu reaches notes without typing a command")
buttons = [b for row in tb.MENU_ROWS for b in row]
check("Notes button present", "🗂 Notes" in buttons, str(buttons))
check("Inbox button present", "📥 Inbox" in buttons, str(buttons))
for label, expect in (("🗂 Notes", "Sliding Window"), ("📥 Inbox", "inbox")):
    upd = FakeUpdate(UID, label)
    handled = asyncio.run(tb._menu_action(upd, FakeCtx([]), UID, label))
    check(f"'{label}' is handled as a menu tap", handled)
    check(f"'{label}' answers with something useful",
          any(expect.lower() in r.lower() for r in upd.message.replies),
          str(upd.message.replies)[:120])
check("every menu button still has a handler",
      all(asyncio.run(tb._menu_action(FakeUpdate(UID, b), FakeCtx([]), UID, b))
          for b in buttons), str(buttons))

print("\n11. A retrieved password never reaches the model or the chat history")
import crypto as _crypto  # noqa: E402
from brain import tools as _t  # noqa: E402

db.save_secret(UID, "gmail", _crypto.encrypt("hunter2-s3cret"), "ankur@example.com")
model_sees = _t.get_password(UID, "gmail")
check("the model is not given the value", "hunter2-s3cret" not in model_sees, model_sees)
check("the model is told where it went", "separate message" in model_sees, model_sees)
held = _t.take_pending_secret(UID)
check("the bot gets the real credential", held and "hunter2-s3cret" in held, str(held)[:40])
check("and it says it self-deletes", "deletes itself" in (held or ""))
check("the queue is emptied by taking it", _t.take_pending_secret(UID) is None)

# What would land in the conversation table is the model's reply, and the model
# only ever saw the placeholder — so nothing to redact.
db.save_turn(UID, "assistant", model_sees)
stored = "\n".join(t["content"] for t in db.recent_turns(UID, limit=50))
check("nothing in stored history contains the secret", "hunter2-s3cret" not in stored)
check("a missing credential is a normal answer, not a leak",
      "No saved credential" in _t.get_password(UID, "nope") and _t.take_pending_secret(UID) is None)


class BurnBot(FakeBot):
    """Sends and deletes like Telegram, so the TTL path can be exercised."""

    def __init__(self):
        super().__init__()
        self.sent_ids = []

    async def send_message(self, chat_id, text, **kw):
        self.sent.append(text)
        self.sent_ids.append(len(self.sent))
        return type("M", (), {"message_id": len(self.sent)})()


async def _secret_flow():
    ctx = FakeCtx([])
    ctx.bot = BurnBot()
    _t.get_password(UID, "gmail")
    delivered = await tb._deliver_secret(ctx, UID, UID)
    return ctx, delivered


ctx, delivered = asyncio.run(_secret_flow())
check("the bot sends the credential itself", delivered and any("hunter2-s3cret" in m for m in ctx.bot.sent))
check("nothing sent when no credential is pending",
      asyncio.run(tb._deliver_secret(FakeCtx([]), UID, UID)) is False)
check("a TTL is set for burning it", tb.SECRET_TTL and tb.SECRET_TTL <= 300, str(tb.SECRET_TTL))
db.delete_secret(UID, "gmail")

print("\n" + ("ALL PASSED" if not fails else f"{len(fails)} FAILED: {fails}"))
sys.exit(1 if fails else 0)
