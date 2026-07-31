"""Re-encrypt everything under a new ENCRYPTION_KEY.

Why this exists: ENCRYPTION_KEY is passed to the image as a build ARG, so it
is printed in plain text in the Coolify build log. Anyone who can read those
logs can decrypt the password vault, the stored Google OAuth tokens and the
mailbox passwords. Changing the env var alone does NOT fix that - every row in
the database is still ciphertext under the OLD key, and the bot would simply
fail to decrypt all of it. The data has to be rewritten, which is what this
does.

Usage (dry run first - it changes nothing and tells you exactly what it would
do):

    python scripts/rotate_encryption_key.py --db /data/brain.db \\
        --old "<current ENCRYPTION_KEY>" --new "<new key>"

    python scripts/rotate_encryption_key.py --db /data/brain.db \\
        --old "..." --new "..." --apply

Generate a new key with:

    python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"

Order of operations, so the bot is never pointed at data it cannot read:
  1. Stop the app (or accept a few seconds of failed decrypts).
  2. Run with --apply. It writes a timestamped backup of the .db first.
  3. Update ENCRYPTION_KEY in Coolify to the new key.
  4. Redeploy.

Safe to re-run: rows that already decrypt under the NEW key are left alone,
so an interrupted run can simply be run again.
"""
from __future__ import annotations

import argparse
import os
import shutil
import sqlite3
import sys
from datetime import datetime

from cryptography.fernet import Fernet, InvalidToken

# (table, primary key column, encrypted column)
TARGETS = [
    ("users", "telegram_id", "custom_sa_enc"),
    ("users", "telegram_id", "custom_oauth_enc"),
    ("users", "telegram_id", "google_token_enc"),
    ("google_accounts", "id", "token_enc"),
    ("mail_accounts", "id", "password_enc"),
    ("secrets", "id", "secret_enc"),
]


def _table_exists(con: sqlite3.Connection, table: str) -> bool:
    row = con.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()
    return row is not None


def _column_exists(con: sqlite3.Connection, table: str, column: str) -> bool:
    cols = {r[1] for r in con.execute(f"PRAGMA table_info({table})")}
    return column in cols


def rotate(db_path: str, old_key: str, new_key: str, apply: bool) -> int:
    if not os.path.exists(db_path):
        print(f"No database at {db_path}", file=sys.stderr)
        return 2
    try:
        old_f, new_f = Fernet(old_key.encode()), Fernet(new_key.encode())
    except Exception as e:  # noqa: BLE001
        print(f"Bad key: {e}", file=sys.stderr)
        return 2
    if old_key == new_key:
        print("Old and new keys are identical - nothing to do.", file=sys.stderr)
        return 2

    if apply:
        backup = f"{db_path}.bak-{datetime.now():%Y%m%d-%H%M%S}"
        shutil.copy2(db_path, backup)
        print(f"Backup written: {backup}")

    con = sqlite3.connect(db_path)
    totals = {"rewritten": 0, "already_new": 0, "unreadable": 0, "empty": 0}

    for table, pk, column in TARGETS:
        if not _table_exists(con, table) or not _column_exists(con, table, column):
            print(f"  skip {table}.{column} (not present)")
            continue
        rows = con.execute(
            f"SELECT {pk}, {column} FROM {table} WHERE {column} IS NOT NULL AND {column} != ''"
        ).fetchall()
        rewritten = already = bad = 0
        for key_val, blob in rows:
            try:
                plain = old_f.decrypt(blob.encode())
            except (InvalidToken, AttributeError, ValueError):
                # Already under the new key? Then this row is simply done.
                try:
                    new_f.decrypt(blob.encode())
                    already += 1
                except Exception:  # noqa: BLE001
                    bad += 1
                    print(f"  !! {table}.{column} {pk}={key_val} decrypts under "
                          "NEITHER key - left untouched")
                continue
            if apply:
                con.execute(
                    f"UPDATE {table} SET {column} = ? WHERE {pk} = ?",
                    (new_f.encrypt(plain).decode(), key_val),
                )
            rewritten += 1
        totals["rewritten"] += rewritten
        totals["already_new"] += already
        totals["unreadable"] += bad
        print(f"  {table}.{column}: {len(rows)} row(s) -> "
              f"{rewritten} to rewrite, {already} already new, {bad} unreadable")

    failures = 0
    if apply:
        con.commit()
        # Prove it: everything must now decrypt under the new key.
        for table, pk, column in TARGETS:
            if not _table_exists(con, table) or not _column_exists(con, table, column):
                continue
            for key_val, blob in con.execute(
                f"SELECT {pk}, {column} FROM {table} "
                f"WHERE {column} IS NOT NULL AND {column} != ''"
            ):
                try:
                    new_f.decrypt(blob.encode())
                except Exception:  # noqa: BLE001
                    failures += 1
                    print(f"  VERIFY FAILED: {table}.{column} {pk}={key_val}")
        if not failures:
            print("\nVerified: every encrypted row decrypts under the new key.")
    con.close()

    # Always print the tally, including on failure - "VERIFY FAILED" with no
    # numbers underneath tells you something broke but not how much.
    print(f"\n{'APPLIED' if apply else 'DRY RUN'} - "
          f"{totals['rewritten']} row(s) {'rewritten' if apply else 'would be rewritten'}, "
          f"{totals['already_new']} already on the new key, "
          f"{totals['unreadable']} unreadable.")
    if not apply:
        print("Re-run with --apply to make the change.")
    if totals["unreadable"] and not failures:
        print("Unreadable rows were left as they are - they are already "
              "undecryptable by the app and need deleting or re-entering.")
    if failures:
        print(f"\n{failures} row(s) do not decrypt under the new key - most "
              "likely --old is not the key this data was encrypted with. "
              "Nothing was rewritten for those rows; restore the backup if "
              "you are unsure, and do NOT change ENCRYPTION_KEY yet.",
              file=sys.stderr)
        return 1
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description="Re-encrypt stored secrets under a new key.")
    p.add_argument("--db", default=os.getenv("BRAIN_DB", "brain.db"),
                   help="Path to the SQLite file (on the server: /data/brain.db)")
    p.add_argument("--old", default=os.getenv("OLD_ENCRYPTION_KEY", ""),
                   help="Current ENCRYPTION_KEY")
    p.add_argument("--new", default=os.getenv("NEW_ENCRYPTION_KEY", ""),
                   help="New ENCRYPTION_KEY")
    p.add_argument("--apply", action="store_true",
                   help="Actually write. Without this it is a dry run.")
    a = p.parse_args()
    if not a.old or not a.new:
        p.error("both --old and --new are required "
                "(or OLD_ENCRYPTION_KEY / NEW_ENCRYPTION_KEY)")
    return rotate(a.db, a.old, a.new, a.apply)


if __name__ == "__main__":
    raise SystemExit(main())
