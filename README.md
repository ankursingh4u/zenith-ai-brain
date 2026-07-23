# Zenith AI Brain 🧠

A secure, multi-user AI assistant on **Telegram** — your personal accountant & second brain.
Talk to it naturally; it tracks money, sets reminders, keeps an encrypted password vault,
reads bill photos, and (per user) connects Gmail / Calendar / Docs / Drive.

## Features

- 💰 **Money & accounting** — "paid electricity 2400", summaries, undo/edit, large-amount checks
- ⏰ **Reminders** — "remind me at 5pm to call the bank" (fires on time)
- 🔐 **Password vault** — encrypted at rest (Fernet)
- 🧾 **Bill photos** — send a pic; vision reads the amount and logs it
- 📊 **Google Sheets** — share a sheet with the bot's service account, records land there
- 📧 **Gmail / 📅 Calendar / 📄 Docs / 📁 Drive** — each user links **their own** Google (multi-account)
- 🔒 **Secure & isolated** — access-code gate, 24h ban on brute force, per-user data isolation,
  **read/write but never deletes** your Google data

## Security model

- **Access gate** — non-owners must enter a secret code (`GODFATHER_ANSWER`); 5 wrong tries = 24h ban.
- **Per-user isolation** — every row is keyed by Telegram ID; no user can see another's data.
- **Each user connects their OWN Google** — either share a sheet with the service account, or link
  their own Gmail accounts via OAuth. No shared/default cross-user access.
- **No deletion** — the bot only reads and writes Google data; it never deletes rows/files.
- **Secrets encrypted** — Google tokens & vault entries are Fernet-encrypted before storage.

---

## Run locally

```bash
python -m venv .venv
# Windows: .\.venv\Scripts\activate    |  Linux/Mac: source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env      # then fill it in (see below)
python main.py
```

Fill `.env`:
- `TELEGRAM_BOT_TOKEN` — from **@BotFather**
- `OPENAI_API_KEY` — from platform.openai.com
- `ENCRYPTION_KEY` — `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`
- `ALLOWED_TELEGRAM_IDS` — your ID (from **@userinfobot**)
- `GODFATHER_ANSWER` — your secret access code
- `GOOGLE_SERVICE_ACCOUNT_FILE` — path to a service-account key (for the shared "share-a-sheet" feature)

---

## Deploy on Coolify 🚀 (no domain needed)

Coolify gives you free HTTPS via an auto-generated `*.sslip.io` URL — perfect since Google OAuth
requires https.

### 1. Push this repo to GitHub (done — see below).

### 2. In Coolify → **New Resource → Application → from your Git repository**
- Pick this repo, branch `main`.
- **Build Pack: Dockerfile** (this repo includes one).
- **Port: `8000`**.

### 3. Set Environment Variables (Coolify → your app → Environment Variables)
Paste each from `.env.example`:
- `TELEGRAM_BOT_TOKEN`, `OPENAI_API_KEY`, `ENCRYPTION_KEY`, `ALLOWED_TELEGRAM_IDS`
- `GODFATHER_QUESTION`, `GODFATHER_ANSWER`
- `GOOGLE_SERVICE_ACCOUNT_JSON` → **paste the whole service-account JSON as one line**
  (use this instead of the file so no secret is committed)
- `TIMEZONE`, `DAILY_JOB_HOUR`, `DUE_REMINDER_DAYS`

### 4. Persistent storage (so user data survives redeploys) ⚠️ important
- Coolify → your app → **Storages → Add** → mount path `/data`.
- Set env var `DATABASE_URL=sqlite:////data/brain.db`.

### 5. Domain / callback URL
- Coolify assigns an HTTPS URL (e.g. `https://brain-xxxx.sslip.io`).
- Set env var `OAUTH_REDIRECT_URI=https://<that-url>/oauth/callback`.

### 6. Deploy.
- Check the logs for `Brain is running`.
- Message your bot on Telegram, enter the access code, and you're live 24/7.

> **Per-user Google login:** each user runs `/connect` in the bot to add their own Google — the bot
> shows a full step-by-step guide with direct links. They create their own OAuth client in Google
> Cloud (redirect URI = your `OAUTH_REDIRECT_URI`), download the JSON, and paste it into the chat.
> Buttons let them add / switch default / remove accounts.

---

## Project layout

```
main.py                 start DB + OAuth callback server + bot + scheduler
config.py  crypto.py  db.py
bot/          auth.py (whitelist) · telegram_bot.py (handlers, gate, buttons)
brain/        agent.py (OpenAI loop) · tools.py · money.py · vision.py
integrations/ gservice.py (service account) · oauth.py (per-user login) · client.py
              sheets.py · drive.py · gmail.py · calendar.py · docs.py · pdf.py
scheduler/    jobs.py (reminders + due-date sweep)
Dockerfile    .dockerignore    requirements.txt    .env.example
```

## Commands
`/start` · `/connect` (Google setup + account buttons) · `/status` · `/whoami` · `/checknow`
