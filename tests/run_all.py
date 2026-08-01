"""Run every suite. Do this before you deploy.

    python tests/run_all.py

Each suite builds its own throwaway SQLite file, so none of this can reach
brain.db or anything on the server. Only test_web_fuzzy.py needs the internet.
"""
from __future__ import annotations

import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SUITES = [
    ("plan CRUD", "test_plan_crud.py"),
    ("conflicts + gaps", "test_conflicts_gaps.py"),
    ("undo + memory fallback", "test_memory_undo.py"),
    ("semantic memory", "test_semantic.py"),
    ("web access + fuzzy", "test_web_fuzzy.py"),
    ("encryption key rotation", "test_key_rotation.py"),
    ("check-in / chasing", "test_checkin.py"),
]


def main() -> int:
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    failed = []
    for label, path in SUITES:
        proc = subprocess.run([sys.executable, os.path.join(HERE, path)],
                              capture_output=True, text=True, env=env,
                              encoding="utf-8", errors="replace")
        tail = [l for l in (proc.stdout or "").splitlines()
                if "ALL PASSED" in l or "FAILED" in l]
        print(f"{label:<26} {tail[-1] if tail else 'NO RESULT'}")
        if proc.returncode != 0:
            failed.append(label)
            for line in (proc.stdout or "").splitlines():
                if "FAIL" in line:
                    print(f"    {line.strip()}")
            if proc.stderr.strip():
                print(f"    stderr: {proc.stderr.strip()[-300:]}")
    print()
    if failed:
        print(f"{len(failed)} suite(s) failed: {', '.join(failed)}")
        return 1
    print("All suites passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
