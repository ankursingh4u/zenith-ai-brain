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
