"""Entry point. Starts the database, the Telegram bot (long-polling), and the
scheduler. Google works via a shared service account — no OAuth server needed.
"""
from __future__ import annotations

import logging
import threading

import uvicorn

import config
import db
from bot.telegram_bot import build_application
from integrations import gservice
from integrations.oauth import app as oauth_app

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(name)s  %(message)s",
)

# httpx logs every request at INFO, and the long-polling URL has the bot token
# in it: "POST https://api.telegram.org/bot<TOKEN>/getUpdates". That put the
# token on every line of the container log, where anyone with log access — the
# hosting UI, an API token, a log drain — could read it. Failures still show up,
# because those are logged at WARNING and above.
logging.getLogger("httpx").setLevel(logging.WARNING)
# The reminder sweep runs every minute and announced itself twice each time.
# Six lines a minute of "nothing happened" buried the startup banner and the
# actual errors; a job that fails still logs at WARNING.
logging.getLogger("apscheduler.executors.default").setLevel(logging.WARNING)

log = logging.getLogger("brain")


def _start_oauth_server() -> None:
    """Run the FastAPI login callback (for Gmail/Calendar/Docs) in a daemon thread."""
    server = uvicorn.Server(
        uvicorn.Config(oauth_app, host="0.0.0.0", port=config.PORT, log_level="warning")
    )
    server.install_signal_handlers = lambda: None
    threading.Thread(target=server.run, daemon=True, name="oauth-server").start()
    log.info("OAuth login server on %s", config.OAUTH_REDIRECT_URI)


def main() -> None:
    db.init_db()
    log.info("Database ready.")

    log.info("LLM: %s | brain=%s vision=%s fast=%s",
             config.LLM_BASE_URL or "api.openai.com (default)",
             config.OPENAI_MODEL, config.VISION_MODEL, config.FAST_MODEL)

    # Answer the embeddings question here, not the first time someone happens
    # to type something — a wrong key otherwise looks exactly like an idle bot.
    from brain import memory
    memory.selftest()

    if gservice.is_configured():
        log.info("Google service account ready: %s", gservice.service_account_email())
    else:
        log.warning("Google service account not set up — sheet/drive features off until "
                    "%s exists. Reminders, vault, transactions still work.",
                    config.GOOGLE_SERVICE_ACCOUNT_FILE)

    # Always run the login callback server. Each user sets up their OWN Google
    # console (paste JSON in Telegram) and links their OWN accounts via /connect.
    _start_oauth_server()
    log.info("Gmail/Calendar/Docs: each user connects their own Google via /connect (fully per-user).")

    app = build_application()
    log.info("Brain is running (build %s). Press Ctrl+C to stop.", config.BUILD)
    # Must include callback_query, or inline button taps never reach the bot.
    app.run_polling(allowed_updates=["message", "callback_query"])


if __name__ == "__main__":
    main()
