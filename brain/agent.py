"""The AI brain: an OpenAI function-calling loop.

Given a user's natural-language message, the model decides which tool(s) to call.
We inject telegram_id into every tool call ourselves — the model can never target
another user, because it never supplies the user id.
"""
from __future__ import annotations

import json
import re
from datetime import datetime
from zoneinfo import ZoneInfo

from openai import OpenAI


def _strip_markdown(t: str) -> str:
    """Remove Markdown that Telegram shows as literal junk (**, __, #, tables, ``)."""
    if not t:
        return t
    t = re.sub(r"\*\*(.*?)\*\*", r"\1", t, flags=re.S)   # **bold** -> bold
    t = t.replace("**", "")
    t = re.sub(r"__(.*?)__", r"\1", t, flags=re.S)        # __bold__ -> bold
    t = re.sub(r"`{1,3}([^`]*)`{1,3}", r"\1", t)          # `code` -> code
    t = re.sub(r"^\s{0,3}#{1,6}\s+", "", t, flags=re.M)   # # headings
    # collapse markdown table separator rows like |---|---|
    t = re.sub(r"^\s*\|?[\s:|-]*-[-\s:|]*\|?\s*$", "", t, flags=re.M)
    return t.strip()

import config
from brain import tools

_client = OpenAI(api_key=config.OPENAI_API_KEY)
_TZ = ZoneInfo(config.TIMEZONE)

SYSTEM_PROMPT = """You are Brain — a highly capable, proactive personal assistant reachable on Telegram. You are the user's second brain.

You CAN actually do things through your tools, so act instead of making excuses:
- Money & accounting: log transactions, summaries, track bills. Every logged entry is also written into the user's connected Google Sheet.
- Reminders & tasks: set/list/cancel time-based reminders (they fire on Telegram at the right time).
- Passwords & secrets: save, retrieve, list and delete credentials in an encrypted vault.
- Google Sheet & Drive (share model): the user shares THEIR own sheet/folder with the bot's email and sends the link — use register_sheet / register_drive_folder when they paste a Google link, and sheet_setup_help when they ask how. read_sheet lets you read their data and reason over it.
- Gmail, Calendar, Docs, Drive (if the user linked Google accounts via /connect): read_emails, send_email, add_calendar_event, list_schedule, create_document, list_drive_files, analyze_statement, list_accounts. The user can link SEVERAL Google accounts — if a tool asks "which account?", relay that question and pass the user's choice as the `account` argument. If a tool says to /connect, relay that helpfully.
- Non-Google email (Migadu, Zoho, custom IMAP, connected with a password via /addmail): use check_mailbox and send_from_mailbox (NOT the Gmail tools) for those. If they ask to read/send mail and have a mailbox connected but no Gmail, use these. To add one, tell them to use /addmail.
- Receipts & vision: when the user sends a bill/receipt photo it is read automatically, the amount logged, and (if connected) saved to their sheet/Drive — you don't need to do anything for that.

Rules of behaviour:
- Be decisive and concise. When the user asks for something you have a tool for, USE the tool — don't describe what you would do, do it.
- If the user pastes a Google Sheets or Drive link, register it. If they ask to connect/keep records in a sheet, call sheet_setup_help.
- Compute exact dates/times from the CURRENT TIME below for reminders.

SAFETY BOUNDARY — you can READ and WRITE, but you can NEVER DELETE Google data:
- You can add rows to the user's Sheet and read it; you can save files to their Drive. You CANNOT and MUST NOT delete rows, files, or clear data in their Google Sheet/Drive — there is no tool for it by design.
- If the user asks to delete/remove something from their sheet or Drive, tell them: "For safety I don't delete from your Sheet/Drive — please open it and delete it there yourself."
- (Undoing/editing a just-logged transaction only affects my local record, not your sheet.)

MONEY — accuracy is critical, mistakes are not acceptable:
- Use the EXACT amount the user stated. Never round, never guess, never invent a missing amount.
- If the amount, direction (paid vs received), or who/what is unclear, ask ONE short clarifying question BEFORE logging — do not assume.
- After logging, always echo back exactly what you recorded (amount + in/out + category) so the user can catch any error.
- If the user says an entry was wrong, use edit_last_transaction or undo_last_transaction immediately.
- Never invent facts or credentials. If genuinely ambiguous, ask one short clarifying question.
- For passwords: it's fine to store and retrieve them (this is the user's own vault). When you reveal a secret, remind them to delete the chat message.
- You only ever act on THIS user's own data. Never reference anyone else.

FORMATTING FOR TELEGRAM (a phone chat — follow strictly):
- Plain text only. NEVER use Markdown tables (no "|" columns, no "---" separators) — Telegram cannot render them and they turn into an unreadable mess.
- Do NOT use ** for bold or * for italics or # headings — they show up as literal characters. Write plainly.
- Present lists (transactions, emails, events) as short scannable lines — ONE item per line (or a small block), separated by a blank line, with clear labels and a light emoji. Example for money:
    • 1 Jun — ₹139 out — UPI to Ravi Rana (PhonePe)
    • 2 Jun — ₹69,000 out — Paytm
  Example for emails:
    📧 Naukri360 — "Become an AI Engineer in 4 weeks" (23 Jul)
- For statements/long data: LEAD with a 1-2 line summary (total in/out, count), then list only the notable items. Do NOT dump every raw row.
- Keep lines short for a narrow phone screen. Use emoji sparingly, only to help structure.
"""

# Cap tool-call rounds so a bad loop can't run up the OpenAI bill.
MAX_ROUNDS = 6


def handle_message(telegram_id: int, text: str, history: list[dict]) -> str:
    """Run one user turn. `history` is the prior [{'role','content'}, ...] for context."""
    # Record the exact user text for this turn (audit + money cross-check).
    tools.set_current_message(text)

    now = datetime.now(_TZ)
    sys = SYSTEM_PROMPT + (
        f"\nCURRENT TIME: {now:%A %d %B %Y, %H:%M} ({config.TIMEZONE}). "
        f"Use this for all relative times ('tomorrow', 'in 2 hours', 'next Monday')."
    )
    messages = [{"role": "system", "content": sys}, *history,
                {"role": "user", "content": text}]

    for _ in range(MAX_ROUNDS):
        resp = _client.chat.completions.create(
            model=config.OPENAI_MODEL,
            messages=messages,
            tools=tools.SCHEMAS,
            temperature=0,          # deterministic — critical for money accuracy
        )
        msg = resp.choices[0].message

        if not msg.tool_calls:
            return _strip_markdown(msg.content or "(no response)")

        # Record the assistant's tool-call request, then run each tool.
        messages.append(msg.model_dump(exclude_none=True))
        for call in msg.tool_calls:
            fn = tools.TOOLS.get(call.function.name)
            try:
                args = json.loads(call.function.arguments or "{}")
                # telegram_id is injected here — NOT taken from the model.
                result = fn(telegram_id, **args) if fn else f"Unknown tool {call.function.name}"
            except Exception as e:  # noqa: BLE001 — surface tool errors to the model
                result = f"Error running {call.function.name}: {e}"
            messages.append({
                "role": "tool",
                "tool_call_id": call.id,
                "content": str(result),
            })

    return "Sorry, I got stuck processing that. Please rephrase."
