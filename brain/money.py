"""Deterministic money-amount checks — a safety net over the AI for accountant work.

The AI decides intent; this module independently re-reads the number from the user's
own words and flags any disagreement, so a misheard amount can't be logged silently.
"""
from __future__ import annotations

import re

from price_parser import Price

# How people here actually write amounts. price_parser reads "2 lakh" as 2,
# which would make the cross-check below fire a false warning on every single
# lakh/crore entry — worse than having no check at all, because it trains you
# to ignore it.
_MULTIPLIERS = {
    "k": 1_000, "thousand": 1_000, "hazaar": 1_000, "hazar": 1_000,
    "lakh": 100_000, "lakhs": 100_000, "lac": 100_000, "lacs": 100_000,
    "l": 100_000,
    "crore": 10_000_000, "crores": 10_000_000, "cr": 10_000_000,
    "million": 1_000_000, "mn": 1_000_000, "m": 1_000_000,
    "billion": 1_000_000_000, "bn": 1_000_000_000,
}
_SCALED = re.compile(
    r"(\d+(?:[.,]\d+)?)\s*(" + "|".join(sorted(_MULTIPLIERS, key=len, reverse=True)) + r")\b",
    re.IGNORECASE,
)


def extract_amount(text: str) -> float | None:
    """Pull the monetary amount from the raw user text, or None if unclear.

    Understands Indian scale words (lakh/crore) and Indian digit grouping
    (2,00,000), because that is how the amounts actually arrive.
    """
    if not text:
        return None
    m = _SCALED.search(text)
    if m:
        try:
            number = float(m.group(1).replace(",", "."))
        except ValueError:
            number = None
        if number is not None:
            return number * _MULTIPLIERS[m.group(2).lower()]
    price = Price.fromstring(text)
    if price.amount is None:
        return None
    return float(price.amount)


def mismatch_warning(ai_amount: float, raw_text: str) -> str | None:
    """Return a warning string if the number the user typed differs from the AI's,
    else None. Tolerates tiny rounding differences.
    """
    typed = extract_amount(raw_text)
    if typed is None:
        return None
    if abs(typed - float(ai_amount)) > max(0.01, 0.001 * typed):
        return (f"⚠️ Please double-check: you wrote something like {typed:g}, "
                f"but I logged {float(ai_amount):g}. Say 'edit last to <amount>' if wrong.")
    return None


# Amounts above this get an explicit confirmation nudge in the reply.
LARGE_AMOUNT = 100000.0
