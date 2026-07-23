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
    log.info("Brain is running. Press Ctrl+C to stop.")
    app.run_polling(allowed_updates=["message"])


if __name__ == "__main__":
    main()
