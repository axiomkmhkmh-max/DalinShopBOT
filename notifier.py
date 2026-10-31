"""Thin holder for the Bot instance so services can notify without importing aiogram handlers."""
from __future__ import annotations

import logging

from config import get_config

log = logging.getLogger(__name__)
_bot = None


def set_bot(bot) -> None:
    global _bot
    _bot = bot


def get_bot():
    return _bot


async def send_user(user_id: int, text: str, **kwargs) -> bool:
    if _bot is None:
        log.warning("notify skipped (no bot): %s", text[:80])
        return False
    try:
        await _bot.send_message(user_id, text, **kwargs)
        return True
    except Exception:  # user blocked the bot, etc.
        log.exception("failed to notify user %s", user_id)
        return False


async def send_admins(text: str) -> None:
    for chat_id in get_config().alert_ids:
        await send_user(chat_id, "⚠️ " + text)
