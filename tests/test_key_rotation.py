"""Key rotation: does it actually re-encrypt everything, and is it safe to re-run?

Runs against a synthetic database only. Never point this at brain.db.
"""
import os
import shutil
import subprocess
import sqlite3
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from cryptography.fernet import Fernet

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(ROOT, "scripts", "rotate_encryption_key.py")

OLD = Fernet.generate_key().decode()
NEW = Fernet.generate_key().decode()
OTHER = Fernet.generate_key().decode()          # a key belonging to nobody
old_f, new_f, other_f = Fernet(OLD.encode()), Fernet(NEW.encode()), Fernet(OTHER.encode())

fails = []


def check(label, cond, detail=""):
    print(("  ok  " if cond else "  FAIL") + f"  {label}" + (f"  <- {detail}" if not cond and detail else ""))
    if not cond:
        fails.append(label)


def build_db(path):
    con = sqlite3.connect(path)
    con.executescript("""
        CREATE TABLE users (telegram_id INTEGER PRIMARY KEY, custom_sa_enc TEXT,
                            custom_oauth_enc TEXT, google_token_enc TEXT);
        CREATE TABLE google_accounts (id INTEGER PRIMARY KEY, token_enc TEXT);
        CREATE TABLE mail_accounts (id INTEGER PRIMARY KEY, password_enc TEXT);
        CREATE TABLE secrets (id INTEGER PRIMARY KEY, secret_enc TEXT);
    """)
    e = lambda s: old_f.encrypt(s.encode()).decode()   # noqa: E731
    con.execute("INSERT INTO users VALUES (?,?,?,?)",
                (1, e("service-account-json"), e("oauth-json"), e("google-token")))
    con.execute("INSERT INTO users VALUES (?,?,?,?)", (2, None, None, e("token-2")))
    con.execute("INSERT INTO google_accounts VALUES (?,?)", (1, e("gtoken")))
    con.execute("INSERT INTO mail_accounts VALUES (?,?)", (1, e("mailpass")))
    for i, secret in enumerate(["wifi-password", "bank-pin", "github-token"], start=1):
        con.execute("INSERT INTO secrets VALUES (?,?)", (i, e(secret)))
    con.commit()
    con.close()


def run(db, old, new, apply=False):
    cmd = [sys.executable, SCRIPT, "--db", db, "--old", old, "--new", new]
    if apply:
        cmd.append("--apply")
    return subprocess.run(cmd, capture_output=True, text=True,
                          encoding="utf-8", errors="replace")


def all_values(db):
    con = sqlite3.connect(db)
    vals = []
    for t, c in [("users", "custom_sa_enc"), ("users", "custom_oauth_enc"),
                 ("users", "google_token_enc"), ("google_accounts", "token_enc"),
                 ("mail_accounts", "password_enc"), ("secrets", "secret_enc")]:
        for (v,) in con.execute(f"SELECT {c} FROM {t} WHERE {c} IS NOT NULL"):
            vals.append(v)
    con.close()
    return vals


tmp = tempfile.mkdtemp()
db = os.path.join(tmp, "brain.db")
build_db(db)

print("\n1. Dry run changes nothing")
before = all_values(db)
r = run(db, OLD, NEW)
check("dry run exits 0", r.returncode == 0, r.stderr[:150])
check("reports 9 rows to rewrite", "9 row(s) would be rewritten" in r.stdout,
      [l for l in r.stdout.splitlines() if "DRY RUN" in l])
check("database untouched", all_values(db) == before)

print("\n2. Apply re-encrypts everything")
r = run(db, OLD, NEW, apply=True)
check("apply exits 0", r.returncode == 0, r.stderr[:200])
check("self-verified", "Verified: every encrypted row decrypts" in r.stdout)
vals = all_values(db)
check("all 9 values changed", all(v not in before for v in vals), "")
ok = all(new_f.decrypt(v.encode()) for v in vals)
check("everything decrypts under the NEW key", ok)
bad = 0
for v in vals:
    try:
        old_f.decrypt(v.encode()); bad += 1
    except Exception:
        pass
check("nothing still readable with the OLD key", bad == 0, f"{bad} rows")

print("\n3. Plaintext survived the round trip intact")
con = sqlite3.connect(db)
got = sorted(new_f.decrypt(v.encode()).decode()
             for (v,) in con.execute("SELECT secret_enc FROM secrets"))
con.close()
check("vault contents unchanged", got == ["bank-pin", "github-token", "wifi-password"], str(got))

print("\n4. A backup was written")
baks = [f for f in os.listdir(tmp) if ".bak-" in f]
check("backup file exists", len(baks) == 1, str(baks))
shutil.copy2(os.path.join(tmp, baks[0]), os.path.join(tmp, "restored.db"))
con = sqlite3.connect(os.path.join(tmp, "restored.db"))
(v,) = con.execute("SELECT secret_enc FROM secrets WHERE id=1").fetchone()
con.close()
check("backup is the pre-rotation state (old key still works on it)",
      old_f.decrypt(v.encode()).decode() == "wifi-password")

print("\n5. Re-running is safe (interrupted-run recovery)")
r = run(db, OLD, NEW, apply=True)
check("second run exits 0", r.returncode == 0, r.stderr[:150])
check("recognises rows are already on the new key", "9 already on the new key" in r.stdout,
      [l for l in r.stdout.splitlines() if "APPLIED" in l])
con = sqlite3.connect(db)
(v,) = con.execute("SELECT secret_enc FROM secrets WHERE id=1").fetchone()
con.close()
check("data still correct after re-run",
      new_f.decrypt(v.encode()).decode() == "wifi-password")

print("\n6. A wrong old key cannot quietly destroy data")
db2 = os.path.join(tmp, "b2.db")
build_db(db2)
snapshot = all_values(db2)
r = run(db2, OTHER, NEW, apply=True)
check("reports every row unreadable", "9 unreadable" in r.stdout,
      [l for l in r.stdout.splitlines() if "APPLIED" in l])
check("exits non-zero so a script/CI notices", r.returncode == 1, str(r.returncode))
check("says the old key is probably wrong", "not the key this data was encrypted with"
      in r.stderr, r.stderr[:120])
check("warns not to change ENCRYPTION_KEY yet", "do NOT change ENCRYPTION_KEY" in r.stderr)
check("left the data alone", all_values(db2) == snapshot)
check("originals still decrypt with the real old key",
      old_f.decrypt(all_values(db2)[0].encode()) is not None)

print("\n7. Guard rails")
r = run(db, OLD, OLD)
check("refuses identical keys", r.returncode == 2 and "identical" in r.stderr, r.stderr[:90])
r = run(db, "not-a-valid-key", NEW)
check("refuses a malformed key", r.returncode == 2 and "Bad key" in r.stderr, r.stderr[:90])
r = run(os.path.join(tmp, "nope.db"), OLD, NEW)
check("refuses a missing database", r.returncode == 2, r.stderr[:90])

print("\n" + ("ALL PASSED" if not fails else f"{len(fails)} FAILED: {fails}"))
sys.exit(1 if fails else 0)
