"""Indian amounts and locale context.

The money cross-check is a safety net over the AI. If it misreads "2 lakh" as
2 it fires a false warning on every large entry, which is worse than no check
at all - it teaches you to ignore the one thing meant to catch a real mistake.
"""
import os
import sys
import tempfile

tmp = os.path.join(tempfile.mkdtemp(), "t.db")
os.environ["DATABASE_URL"] = f"sqlite:///{tmp}"
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "x")
os.environ.setdefault("OPENAI_API_KEY", "x")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from cryptography.fernet import Fernet
os.environ["ENCRYPTION_KEY"] = Fernet.generate_key().decode()

import config
from brain import money

fails = []


def check(label, cond, detail=""):
    print(("  ok  " if cond else "  FAIL") + f"  {label}" + (f"  <- {detail}" if not cond and detail else ""))
    if not cond:
        fails.append(label)


print("\n1. Indian scale words")
CASES = [
    ("paid 2 lakh for the car", 200_000),
    ("2.5 lakh", 250_000),
    ("2.5 lakhs to builder", 250_000),
    ("3 lac advance", 300_000),
    ("1 crore", 10_000_000),
    ("1.2 crore for the flat", 12_000_000),
    ("2 cr", 20_000_000),
    ("50k rent", 50_000),
    ("15 thousand", 15_000),
]
for text, want in CASES:
    got = money.extract_amount(text)
    check(f"{text!r} -> {want:,}", got == want, f"got {got}")

print("\n2. Plain amounts still work (no regression)")
PLAIN = [
    ("1500", 1500), ("paid 45000", 45000), ("rs 1,500.50", 1500.5),
    ("2,00,000 to Ravi", 200_000), ("450 for food", 450),
    ("electricity 2400", 2400),
]
for text, want in PLAIN:
    got = money.extract_amount(text)
    check(f"{text!r} -> {want:,}", got == want, f"got {got}")

print("\n3. No amount means no amount")
for text in ["remind me tomorrow", "how much did I spend?", ""]:
    check(f"{text!r} -> None", money.extract_amount(text) is None,
          str(money.extract_amount(text)))

print("\n4. The cross-check no longer cries wolf on Indian amounts")
# This is the bug: user types "2 lakh", AI correctly logs 200000, and the
# old checker compared 200000 against 2 and warned every time.
check("2 lakh vs 200000 -> no warning",
      money.mismatch_warning(200_000, "paid 2 lakh for the car") is None)
check("1 crore vs 10000000 -> no warning",
      money.mismatch_warning(10_000_000, "1 crore") is None)
check("50k vs 50000 -> no warning",
      money.mismatch_warning(50_000, "50k rent") is None)

print("\n5. It still catches a genuine mistake")
w = money.mismatch_warning(20_000, "paid 2 lakh for the car")
check("2 lakh logged as 20000 IS flagged", w is not None and "double-check" in w,
      str(w))
w2 = money.mismatch_warning(4500, "electricity 2400")
check("2400 logged as 4500 IS flagged", w2 is not None, str(w2))

print("\n6. Locale config defaults to India")
check("country", config.COUNTRY == "India", config.COUNTRY)
check("timezone", config.TIMEZONE == "Asia/Kolkata", config.TIMEZONE)
check("currency", config.CURRENCY == "INR", config.CURRENCY)
check("day-first dates", config.DATE_ORDER.startswith("DD"), config.DATE_ORDER)

print("\n7. The locale block reaches the model every turn")
import db
from brain import agent
db.init_db()
db.get_or_create_user(4242, "T")
captured = {}


def fake_complete(**kw):
    captured["system"] = kw["messages"][0]["content"]
    raise RuntimeError("stop here - we only wanted the prompt")


agent._complete = fake_complete
try:
    agent._run_turn(4242, "hello", [])
except RuntimeError:
    pass
sysmsg = captured.get("system", "")
check("names the country", "India" in sysmsg)
check("names the timezone", "Asia/Kolkata" in sysmsg)
check("explains day-first dates", "3 April, not 4 March" in sysmsg)
check("explains lakh and crore", "lakh" in sysmsg and "crore" in sysmsg)
check("covers Hinglish time words", "subah" in sysmsg and "parso" in sysmsg)
check("forbids guessing a bare hour", "BARE HOUR IS AMBIGUOUS" in sysmsg)
check("requires echoing the time back", "echo back what you set" in sysmsg)
check("tells it to match the sheet's format", "MATCH THE SHEET'S OWN CONVENTIONS" in sysmsg)
check("still carries the current time", "CURRENT TIME:" in sysmsg)

print("\n" + ("ALL PASSED" if not fails else f"{len(fails)} FAILED: {fails}"))
sys.exit(1 if fails else 0)
