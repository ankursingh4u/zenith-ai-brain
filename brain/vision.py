"""Read a bill/receipt image with OpenAI vision and extract structured fields."""
from __future__ import annotations

import base64
import json

from openai import OpenAI

import config

_client = OpenAI(api_key=config.OPENAI_API_KEY)

_PROMPT = (
    "You are reading a photo of a bill, receipt, or payment screenshot. "
    "Extract the payment details. Respond with ONLY a JSON object with keys: "
    "merchant (string), amount (number, the total paid), "
    "kind ('out' for a payment/expense, 'in' for money received), "
    "category (short word like electricity, food, fuel, rent, shopping), "
    "date (YYYY-MM-DD if visible else empty), "
    "note (any useful detail). "
    "If you cannot read an amount, set amount to 0."
)


_COLUMN_PROMPT = (
    "You are filling ONE row of the user's existing spreadsheet from a payment "
    "screenshot plus what the user typed.\n\n"
    "Return ONLY a JSON object whose keys are EXACTLY the column names given below "
    "(copy them character for character). Rules:\n"
    "- Follow the formatting of the existing rows: same date format, same "
    "capitalisation, same abbreviations (e.g. 'PAYTM UPI', 'AAKASH IDFC ACC.').\n"
    "- If a column has the same value in every existing row (like an employee "
    "name), reuse that value.\n"
    "- Take the amount, payer and payee from the screenshot; take the reason and "
    "any correction from what the user typed — the user's words win on the reason "
    "and on who it was sent to.\n"
    "- OMIT a column entirely if you cannot determine it. Never guess an amount, "
    "never invent a name. Do not add keys that aren't in the column list."
)


def read_for_columns(
    image_bytes: bytes, headers: list[str], sample_rows: list[list],
    caption: str = "", mime: str = "image/jpeg",
) -> dict:
    """Read the screenshot in the context of a real sheet: its columns and the
    rows already in it. Returns {column_name: value} for the columns it could fill.
    """
    if not headers:
        return {}
    sample = "\n".join("\t".join(str(c) for c in r) for r in sample_rows[-8:])
    context = (
        f"{_COLUMN_PROMPT}\n\n"
        f"COLUMNS: {' | '.join(headers)}\n\n"
        f"EXISTING ROWS (tab-separated, same column order) — copy their style:\n"
        f"{sample or '(none yet)'}\n\n"
        f"WHAT THE USER TYPED WITH THE SCREENSHOT: {caption or '(nothing)'}"
    )
    b64 = base64.b64encode(image_bytes).decode()
    resp = _client.chat.completions.create(
        model=config.OPENAI_MODEL,
        messages=[{
            "role": "user",
            "content": [
                {"type": "text", "text": context},
                {"type": "image_url",
                 "image_url": {"url": f"data:{mime};base64,{b64}"}},
            ],
        }],
        temperature=0,
        response_format={"type": "json_object"},
    )
    try:
        data = json.loads(resp.choices[0].message.content or "{}")
    except Exception:  # noqa: BLE001
        return {}
    if not isinstance(data, dict):
        return {}
    # Keep only real columns, drop empties.
    allowed = {h.strip().lower(): h for h in headers}
    return {allowed[k.strip().lower()]: str(v).strip()
            for k, v in data.items()
            if k.strip().lower() in allowed and str(v).strip() not in ("", "None", "null")}


def read_bill(image_bytes: bytes, mime: str = "image/jpeg") -> dict:
    """Return {merchant, amount, kind, category, date, note}. amount=0 if unreadable."""
    b64 = base64.b64encode(image_bytes).decode()
    resp = _client.chat.completions.create(
        model=config.OPENAI_MODEL,           # gpt-4o-mini supports vision
        messages=[{
            "role": "user",
            "content": [
                {"type": "text", "text": _PROMPT},
                {"type": "image_url",
                 "image_url": {"url": f"data:{mime};base64,{b64}"}},
            ],
        }],
        temperature=0,
        response_format={"type": "json_object"},
    )
    try:
        data = json.loads(resp.choices[0].message.content or "{}")
    except Exception:  # noqa: BLE001
        return {"amount": 0}
    return data
