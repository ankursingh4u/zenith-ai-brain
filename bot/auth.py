"""Whitelist gate. Only approved Telegram IDs may use the bot at all."""
from __future__ import annotations

import config


def is_allowed(telegram_id: int) -> bool:
    # Empty whitelist = locked down (nobody), on purpose. Fill ALLOWED_TELEGRAM_IDS.
    return telegram_id in config.ALLOWED_TELEGRAM_IDS
