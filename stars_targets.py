"""Destination account for Telegram Stars orders: @username validation (FORMAT ONLY).

HONEST LIMIT: the Telegram Bot API cannot tell us whether a *user* @username exists (getChat only resolves public
channels/groups). So we validate the format here, store the username on the order, and show it to whoever delivers.
Never ask the customer for a password, a login code or a 2FA password - the only thing we need is the public @username.
"""
from __future__ import annotations

import re

# Telegram usernames: 5..32 chars, latin letters / digits / underscore, must start with a letter, must not end with "_".
_USERNAME_RE = re.compile(r"[A-Za-z][A-Za-z0-9_]{3,30}[A-Za-z0-9]")
_DIGITS = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")
_PREFIXES = ("https://t.me/", "http://t.me/", "https://telegram.me/", "t.me/", "@")

MSG_INVALID = ("یوزرنیم نامعتبر است. باید ۵ تا ۳۲ کاراکتر باشد (حروف انگلیسی، عدد و _)، با حرف شروع شود و با _ تمام نشود. "
               "مثال: @username")


class InvalidUsername(ValueError):
    pass


def normalize_username(raw: str) -> str:
    """'@Name', 't.me/Name' or 'Name' -> 'Name' (case kept). Raises InvalidUsername."""
    text = (raw or "").strip().translate(_DIGITS)
    low = text.lower()
    for prefix in _PREFIXES:
        if low.startswith(prefix):
            text = text[len(prefix):]
            break
    text = text.split("?", 1)[0].strip().rstrip("/")
    if not _USERNAME_RE.fullmatch(text):
        raise InvalidUsername(MSG_INVALID)
    return text
