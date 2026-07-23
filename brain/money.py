"""Deterministic money-amount checks — a safety net over the AI for accountant work.

The AI decides intent; this module independently re-reads the number from the user's
own words and flags any disagreement, so a misheard amount can't be logged silently.
"""
from __future__ import annotations

from price_parser import Price


def extract_amount(text: str) -> float | None:
    """Pull the monetary amount from the raw user text, or None if unclear."""
    if not text:
        return None
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
