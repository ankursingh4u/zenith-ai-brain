"""Pick which sheet tab an entry belongs in when the user didn't name one.

Naming a tab always wins — this only runs as the fallback, and it decides from
the tab names, their column headers and what was read off the receipt. If it
isn't reasonably sure it returns None so the caller falls back to the first tab.
"""
from __future__ import annotations

import json

from openai import OpenAI

import config

_client = OpenAI(api_key=config.OPENAI_API_KEY)

_PROMPT = (
    "You route a payment entry into the right tab of a bookkeeping spreadsheet.\n"
    "Given the tabs (with their column headers), what the user wrote and what was "
    "read off the payment screenshot, choose the ONE tab it belongs in.\n"
    'Answer with ONLY JSON: {"tab": "<exact tab name>", "confidence": 0.0-1.0, '
    '"why": "<a few words>"}\n'
    "Judge by what the columns imply: a card/bill settlement belongs in a bill "
    "payments tab, a bank-to-bank transfer in a transfer tab, a card swipe at a "
    "shop in a swipe tab, cash paid into an account in a deposit tab, and ordinary "
    "spending in the expenses tab.\n"
    "If genuinely unsure, give a low confidence — a wrong tab is worse than the default."
)

MIN_CONFIDENCE = 0.6


def choose_tab(tabs: dict[str, list[str]], caption: str, facts: dict) -> tuple[str | None, str]:
    """tabs = {tab_name: [column headers]}. Returns (tab_or_None, reason)."""
    if not tabs:
        return None, "no tabs"
    if len(tabs) == 1:
        only = next(iter(tabs))
        return only, "only one tab"

    layout = "\n".join(
        f"- {name}: {', '.join(cols) if cols else '(no header row)'}"
        for name, cols in tabs.items()
    )
    detail = ", ".join(f"{k}={v}" for k, v in facts.items() if v not in (None, ""))
    try:
        resp = _client.chat.completions.create(
            model=config.OPENAI_MODEL,
            messages=[{"role": "user", "content": (
                f"{_PROMPT}\n\nTABS:\n{layout}\n\n"
                f"USER WROTE: {caption or '(nothing)'}\n"
                f"READ FROM SCREENSHOT: {detail or '(nothing)'}"
            )}],
            temperature=0,
            response_format={"type": "json_object"},
        )
        data = json.loads(resp.choices[0].message.content or "{}")
    except Exception as e:  # noqa: BLE001 — routing is best-effort, never fatal
        return None, f"router failed: {e}"

    pick = str(data.get("tab") or "").strip()
    try:
        conf = float(data.get("confidence") or 0)
    except (TypeError, ValueError):
        conf = 0.0
    match = next((t for t in tabs if t.lower() == pick.lower()), None)
    if match is None:
        return None, f"picked an unknown tab {pick!r}"
    if conf < MIN_CONFIDENCE:
        return None, f"unsure about {match} ({conf:.2f})"
    return match, f"{data.get('why') or 'matched'} ({conf:.2f})"
