"""Telegram bot (long-polling — no public webhook needed for the bot itself)."""
from __future__ import annotations

import asyncio
from datetime import datetime

from telegram import BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application, CallbackQueryHandler, CommandHandler, ContextTypes,
    MessageHandler, filters
)

import config
import crypto
import db
from bot.auth import is_allowed
from brain import agent, tools, vision
from integrations import drive, gservice, oauth, sheets
from scheduler.jobs import run_daily_check, start_scheduler

WELCOME = (
    "🧠 Access granted. I'm *Brain* — your personal assistant & accountant.\n\n"
    "Here's what I can do — just talk naturally:\n"
    "💰 Track money — \"paid electricity 2400\", \"received 5000 from client\", \"summary this month\"\n"
    "⏰ Remind you — \"remind me at 5pm to call the bank\"\n"
    "🔐 Keep passwords safe (encrypted) — \"save my wifi password\", \"what's my gmail password\"\n"
    "🧾 Read bill photos — just send a photo, I'll read the amount and log it\n"
    "📊 Record everything in your own Google Sheet — share your sheet with me and send the link\n"
    "✏️ Fix mistakes instantly — \"undo that\", \"edit last to 2500\"\n\n"
    "What can I do for you?"
)


# Pending yes/no confirmations for override actions, keyed by telegram_id.
_pending: dict[int, dict] = {}

_YES = {"yes", "y", "confirm", "confirmed", "ok", "okay", "haan", "ha", "haa", "yep", "sure"}


# --- Access control -------------------------------------------------------
def _authorized(uid: int) -> bool:
    return is_allowed(uid) or db.is_verified(uid)


async def _gate(update: Update) -> bool:
    """True if the user may proceed. Otherwise runs the code check / ban and returns False."""
    uid = update.effective_user.id
    if _authorized(uid):
        return True
    if not update.message:
        return False

    # Currently banned?
    left = db.banned_seconds_left(uid)
    if left > 0:
        h, m = left // 3600, (left % 3600) // 60
        await update.message.reply_text(f"⛔ Too many wrong codes. Try again in {h}h {m}m.")
        return False

    text = (update.message.text or "").strip()
    if text and text.lower() == config.GODFATHER_ANSWER.lower():   # exact secret code
        db.set_verified(uid, update.effective_user.full_name)
        db.reset_failed_code(uid)
        await update.message.reply_text(WELCOME, parse_mode="Markdown")
    elif text:
        used, banned = db.record_failed_code(
            uid, update.effective_user.full_name, config.MAX_CODE_ATTEMPTS, config.BAN_HOURS)
        if banned:
            await update.message.reply_text(
                f"⛔ {config.MAX_CODE_ATTEMPTS} wrong attempts. You're banned for {config.BAN_HOURS} hours.")
        else:
            await update.message.reply_text(
                f"❌ Wrong code. {config.MAX_CODE_ATTEMPTS - used} attempt(s) left.\n{config.GODFATHER_QUESTION}")
    else:  # non-text (e.g. a photo) from an unverified user
        await update.message.reply_text(config.GODFATHER_QUESTION)
    return False


def _looks_like_service_account(text: str) -> bool:
    t = text or ""
    return '"type"' in t and "service_account" in t and "private_key" in t


def _looks_like_oauth_client(text: str) -> bool:
    t = text or ""
    return ('"client_secret"' in t and '"client_id"' in t
            and ('"web"' in t or '"installed"' in t or '"auth_uri"' in t)
            and '"private_key"' not in t)


async def _apply_oauth_client(update: Update, ctx: ContextTypes.DEFAULT_TYPE, uid: int, text: str) -> None:
    """A pasted OAuth client_secret JSON → console credentials.
    Owner + 'default' → the shared default for all new users; otherwise → this user's own."""
    key_json = _extract_json(text)
    try:
        await ctx.bot.delete_message(update.effective_chat.id, update.message.message_id)
    except Exception:  # noqa: BLE001
        pass
    ok, info = oauth.save_client_json(key_json, telegram_id=uid)
    if not ok:
        return await ctx.bot.send_message(update.effective_chat.id, f"⚠️ That wasn't a valid OAuth client JSON: {info}")
    await ctx.bot.send_message(
        update.effective_chat.id,
        "✅ Your Google console is set up — only you control it. Send /connect to link "
        "your account(s) — email, calendar, docs, drive.\n(I deleted your pasted config for safety.)")


def _extract_json(text: str) -> str:
    """Pull the JSON object out of a message that may have words around it."""
    i, j = text.find("{"), text.rfind("}")
    return text[i:j + 1] if (i != -1 and j > i) else text


async def _apply_custom_key(update: Update, ctx: ContextTypes.DEFAULT_TYPE, uid: int, text: str) -> None:
    """User pasted a service-account key JSON (optionally with words like 'make default')."""
    key_json = _extract_json(text)
    # Delete the pasted key from chat immediately (it contains a private key).
    try:
        await ctx.bot.delete_message(update.effective_chat.id, update.message.message_id)
    except Exception:  # noqa: BLE001
        pass

    ok, info = gservice.validate_key_json(key_json)
    if not ok:
        return await ctx.bot.send_message(update.effective_chat.id, f"⚠️ That key didn't work: {info}")

    # Natural-language intent: "replace/make this the default (for everyone)".
    lowered = text.lower()
    wants_default = any(k in lowered for k in
                        ("default", "everyone", "for all", "global", "shared", "replace"))

    if wants_default and is_allowed(uid):   # only an owner can change the global key
        # Override = ask for confirmation first.
        _pending[uid] = {"type": "default_key", "data": key_json, "email": info}
        await ctx.bot.send_message(
            update.effective_chat.id,
            "⚠️ This will REPLACE the shared Google key for EVERYONE with the one you pasted.\n"
            f"New shared email would be:\n{info}\n\nReply YES to confirm, or NO to cancel.\n"
            "(I deleted your pasted key for safety; it's held only until you confirm.)")
    else:
        db.set_custom_sa(uid, crypto.encrypt(key_json))
        await ctx.bot.send_message(
            update.effective_chat.id,
            "✅ Your own Google is now connected (just for you, full control).\n"
            f"Share your Sheet/Drive folder with:\n{info}\nthen send me the link.\n"
            "(I deleted your pasted key for safety.)")


async def _handle_pending(update: Update, ctx: ContextTypes.DEFAULT_TYPE, uid: int, text: str) -> bool:
    """If the user has a pending confirmation, resolve it. Returns True if handled."""
    if uid not in _pending:
        return False
    pend = _pending.pop(uid)
    if text.strip().lower() in _YES:
        if pend["type"] == "default_key":
            ok, info = gservice.set_default_key(pend["data"])
            msg = (f"✅ Done — the shared Google key is now updated for everyone.\nNew email:\n{info}"
                   if ok else f"⚠️ Couldn't apply: {info}")
            await update.message.reply_text(msg)
    else:
        await update.message.reply_text("Cancelled — nothing was changed.")
    return True


# --- Commands -------------------------------------------------------------
async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _gate(update):
        return
    db.get_or_create_user(update.effective_user.id, update.effective_user.full_name)
    await update.message.reply_text(WELCOME, parse_mode="Markdown")


async def whoami(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(f"Your Telegram ID: {update.effective_user.id}")


def _setup_guide(redirect: str) -> str:
    """Full Google Console setup guide with direct links (for full email/drive/docs access)."""
    return (
        "📖 FULL-ACCESS SETUP (email · drive · docs · calendar)\n"
        "Do this once, ~8 min. It's YOUR own Google — private to you.\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "STEP 1 — Create a project\n"
        "https://console.cloud.google.com/projectcreate\n"
        "→ name it 'Brain', click Create.\n\n"
        "STEP 2 — Turn on 5 APIs (open each, click ENABLE)\n"
        "Gmail: https://console.cloud.google.com/apis/library/gmail.googleapis.com\n"
        "Drive: https://console.cloud.google.com/apis/library/drive.googleapis.com\n"
        "Calendar: https://console.cloud.google.com/apis/library/calendar-json.googleapis.com\n"
        "Docs: https://console.cloud.google.com/apis/library/docs.googleapis.com\n"
        "Sheets: https://console.cloud.google.com/apis/library/sheets.googleapis.com\n\n"
        "STEP 3 — Consent screen\n"
        "https://console.cloud.google.com/auth/overview\n"
        "→ choose External + Testing, then add your Gmail as a TEST USER.\n\n"
        "STEP 4 — Create the login key\n"
        "https://console.cloud.google.com/apis/credentials\n"
        "→ Create Credentials → OAuth client ID → type: Web application.\n"
        "→ Under 'Authorized redirect URIs' add EXACTLY this line:\n"
        f"{redirect}\n\n"
        "STEP 5 — Send it to me\n"
        "→ Click ⬇ DOWNLOAD JSON, open the file, copy ALL the text, and PASTE it here.\n"
        "→ I set it up just for you and delete your message.\n\n"
        "STEP 6 — Log in\n"
        "→ Send /connect again → tap 'Add account' → approve. Repeat to add more."
    )


def _accounts_keyboard(uid: int) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton("➕ Add Google account", callback_data="conn:add")]]
    if db.list_google_accounts(uid):
        rows.append([InlineKeyboardButton("🔄 Change default account", callback_data="conn:setdef")])
        rows.append([InlineKeyboardButton("🗑 Remove an account", callback_data="conn:remove")])
    rows.append([InlineKeyboardButton("📖 Full-access setup guide", callback_data="conn:guide")])
    return InlineKeyboardMarkup(rows)


async def connect(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Account manager: show sheet help, linked accounts + Add/Switch/Remove buttons."""
    if not await _gate(update):
        return
    uid = update.effective_user.id
    db.get_or_create_user(uid, update.effective_user.full_name)

    lines = ["🔗 CONNECT GOOGLE\n",
             "📊 Basic (just a Sheet, no login):", tools.sheet_setup_help(uid), ""]
    accts = db.list_google_accounts(uid)
    default = db.get_default_account(uid)
    if accts:
        lines.append("📧 Your linked Google accounts:")
        for a in accts:
            lines.append(f"• {a.email}" + ("  ⭐ default" if a.email == default else ""))
        lines.append("\nUse the buttons below to add, switch default, or remove.")
    else:
        lines.append("📧 No Google accounts linked yet — tap ➕ below to set one up "
                     "(for email/drive/docs/calendar).")
    await update.message.reply_text("\n".join(lines), reply_markup=_accounts_keyboard(uid),
                                    disable_web_page_preview=True)


async def _send_add_flow(chat_id: int, uid: int, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Give the login link if their console is set, else the setup guide."""
    if oauth.is_configured(uid):
        try:
            url = oauth.build_login_url(uid)
            await ctx.bot.send_message(
                chat_id, f"📧 Tap to log in and link an account:\n{url}",
                disable_web_page_preview=True)
            return
        except RuntimeError:
            pass
    await ctx.bot.send_message(chat_id, _setup_guide(config.OAUTH_REDIRECT_URI),
                              disable_web_page_preview=True)


async def on_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    uid = q.from_user.id
    if not _authorized(uid):
        return await q.answer("Not authorised.", show_alert=True)
    await q.answer()
    data = q.data or ""
    chat_id = q.message.chat_id

    if data in ("conn:add", "conn:guide"):
        await _send_add_flow(chat_id, uid, ctx)

    elif data == "conn:setdef":
        accts = db.list_google_accounts(uid)
        if not accts:
            return await ctx.bot.send_message(chat_id, "No accounts to set. Tap ➕ Add first.")
        kb = [[InlineKeyboardButton(a.email, callback_data=f"sd:{a.email}")] for a in accts]
        await ctx.bot.send_message(chat_id, "Pick your default account:",
                                   reply_markup=InlineKeyboardMarkup(kb))

    elif data.startswith("sd:"):
        email = data[3:]
        if any(a.email == email for a in db.list_google_accounts(uid)):
            db.set_default_account(uid, email)
            await ctx.bot.send_message(chat_id, f"⭐ Default set to {email}. I'll use it unless you name another.")
        else:
            await ctx.bot.send_message(chat_id, "That account isn't linked anymore.")

    elif data == "conn:remove":
        accts = db.list_google_accounts(uid)
        if not accts:
            return await ctx.bot.send_message(chat_id, "No accounts to remove.")
        kb = [[InlineKeyboardButton(f"🗑 {a.email}", callback_data=f"rm:{a.email}")] for a in accts]
        await ctx.bot.send_message(chat_id, "Pick an account to remove:",
                                   reply_markup=InlineKeyboardMarkup(kb))

    elif data.startswith("rm:"):
        email = data[3:]
        if db.remove_google_account(uid, email):
            if db.get_default_account(uid) == email:
                db.set_default_account(uid, None)
            await ctx.bot.send_message(chat_id, f"🗑 Removed {email}.")
        else:
            await ctx.bot.send_message(chat_id, "Couldn't find that account.")


async def accounts(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Quick account switcher: list linked Google accounts + Add/Switch/Remove buttons."""
    if not await _gate(update):
        return
    uid = update.effective_user.id
    db.get_or_create_user(uid, update.effective_user.full_name)
    accts = db.list_google_accounts(uid)
    default = db.get_default_account(uid)
    if not accts:
        return await update.message.reply_text(
            "You haven't linked any Google account yet. Tap ➕ to add one.",
            reply_markup=_accounts_keyboard(uid))
    lines = ["📧 Your Google accounts:"]
    for a in accts:
        lines.append(f"• {a.email}" + ("  ⭐ default (used for mail/drive/etc.)" if a.email == default else ""))
    lines.append("\nTap 🔄 to change which account I use, or ➕ to add another.")
    await update.message.reply_text("\n".join(lines), reply_markup=_accounts_keyboard(uid))


async def status(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _gate(update):
        return
    uid = update.effective_user.id
    sheet_id, folder_id = db.get_user_resources(uid)
    accts = db.list_google_accounts(uid)
    lines = [
        f"📊 Sheet: {'✅ connected' if sheet_id else '❌ not connected'}",
        f"📁 Drive folder: {'✅ connected' if folder_id else '➖ not set (optional)'}",
        "📧 Google accounts (email/calendar/docs/drive): "
        + (", ".join(a.email for a in accts) if accts else "none — /connect to add"),
    ]
    await update.message.reply_text("\n".join(lines))


# --- Messages -------------------------------------------------------------
async def on_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _gate(update):
        return
    uid = update.effective_user.id
    text = update.message.text

    # Resolve any pending override confirmation first (yes/no).
    if await _handle_pending(update, ctx, uid, text):
        return

    # Anyone can paste an OAuth client JSON → their own console (owner can make it default).
    if _looks_like_oauth_client(text):
        return await _apply_oauth_client(update, ctx, uid, text)

    # Hidden: a pasted service-account key configures the user's own Google.
    if _looks_like_service_account(text):
        return await _apply_custom_key(update, ctx, uid, text)

    db.get_or_create_user(uid, update.effective_user.full_name)
    await ctx.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")

    # Persistent, per-user context (isolated by telegram_id, survives restarts).
    hist = db.recent_turns(uid, limit=12)
    try:
        reply = await asyncio.to_thread(agent.handle_message, uid, text, hist)
    except Exception as e:  # noqa: BLE001
        reply = f"⚠️ Something went wrong: {e}"

    db.save_turn(uid, "user", text)
    db.save_turn(uid, "assistant", reply)
    await update.message.reply_text(reply)


async def on_photo(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """A photo/document = a bill. Read it with vision, log it, save to sheet/Drive."""
    if not await _gate(update):
        return
    uid = update.effective_user.id
    db.get_or_create_user(uid, update.effective_user.full_name)

    msg = update.message
    if msg.photo:
        tg_file = await msg.photo[-1].get_file()          # largest size
        ext, mime = "jpg", "image/jpeg"
    else:  # document (PDF etc.)
        tg_file = await msg.document.get_file()
        name = msg.document.file_name or "bill"
        ext = name.rsplit(".", 1)[-1] if "." in name else "bin"
        mime = msg.document.mime_type or "application/octet-stream"

    content = bytes(await tg_file.download_as_bytearray())
    caption = (msg.caption or "bill").strip().replace(" ", "_")[:40]
    filename = f"{datetime.now():%Y%m%d_%H%M%S}_{caption}.{ext}"

    await ctx.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")

    # 1) Read the bill with vision (works even before Google is connected).
    parsed = {}
    if mime.startswith("image/"):
        try:
            parsed = await asyncio.to_thread(vision.read_bill, content, mime)
        except Exception:  # noqa: BLE001
            parsed = {}

    # 2) Auto-log the transaction if an amount was read.
    logged_line = ""
    try:
        amount = float(parsed.get("amount") or 0)
    except (TypeError, ValueError):
        amount = 0.0
    if amount > 0:
        with db.session() as s:
            s.add(db.Transaction(
                telegram_id=uid, amount=amount,
                kind="in" if parsed.get("kind") == "in" else "out",
                category=(parsed.get("category") or "")[:80] or None,
                note=(parsed.get("merchant") or parsed.get("note") or "bill")[:200],
            ))
            s.commit()
        merch = parsed.get("merchant") or "bill"
        logged_line = f"\n🧠 Read it: {merch} — {amount:.2f}. Logged."

        # Mirror to the user's connected sheet (if any).
        sheet_id, _ = db.get_user_resources(uid)
        if sheet_id:
            try:
                await asyncio.to_thread(
                    sheets.append_transaction, uid, amount,
                    "in" if parsed.get("kind") == "in" else "out",
                    parsed.get("category"), parsed.get("merchant") or "bill",
                )
                logged_line += " → saved to your sheet"
            except Exception:  # noqa: BLE001
                logged_line += " (⚠️ couldn't write to your sheet)"

    # 3) Save the image into the user's connected Drive folder (if any).
    drive_line = ""
    _, folder_id = db.get_user_resources(uid)
    if folder_id:
        try:
            link = await asyncio.to_thread(drive.upload_file, uid, filename, content, mime)
            drive_line = f"\n📁 Saved to your Drive: {link}"
        except Exception as e:  # noqa: BLE001
            drive_line = f"\n⚠️ Couldn't save to Drive: {e}"

    await msg.reply_text("🧾 Bill received." + logged_line + drive_line)


async def checknow(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Manually trigger the due-date sweep (for testing)."""
    if not await _gate(update):
        return
    await update.message.reply_text("🔎 Running due-date checks now...")
    await run_daily_check(ctx.bot)
    await update.message.reply_text("✅ Checks done.")


async def _post_init(app: Application) -> None:
    await app.bot.set_my_commands([
        BotCommand("start", "What I can do"),
        BotCommand("connect", "Connect a Sheet or your Google account"),
        BotCommand("accounts", "Switch or add Google accounts"),
        BotCommand("status", "See what you have connected"),
        BotCommand("whoami", "Show my Telegram ID"),
    ])
    start_scheduler(app)


def build_application() -> Application:
    app = (
        Application.builder()
        .token(config.TELEGRAM_BOT_TOKEN)
        .post_init(_post_init)
        .build()
    )
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("whoami", whoami))
    app.add_handler(CommandHandler("connect", connect))
    app.add_handler(CommandHandler("accounts", accounts))
    app.add_handler(CommandHandler("status", status))
    app.add_handler(CommandHandler("checknow", checknow))
    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_handler(MessageHandler(filters.PHOTO | filters.Document.ALL, on_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_message))
    return app
