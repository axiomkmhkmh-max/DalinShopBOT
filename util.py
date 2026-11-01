from __future__ import annotations

import uuid
from datetime import datetime, timezone

_FA = str.maketrans("0123456789", "۰۱۲۳۴۵۶۷۸۹")


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def new_id() -> str:
    return uuid.uuid4().hex


def fa_digits(text) -> str:
    return str(text).translate(_FA)


def fmt_toman(amount: int) -> str:
    return fa_digits(f"{int(amount):,}").replace(",", "٬") + " تومان"


def as_aware(dt):
    """mongomock/pymongo may return naive datetimes; treat them as UTC."""
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def redact(text) -> str:
    """Remove secrets from any text that is stored, logged or shown in the dashboard."""
    from config import get_config

    out = str(text)
    cfg = get_config()
    for secret in (cfg.bot_token, cfg.rate_api_key, cfg.session_secret, cfg.webhook_secret,
                   cfg.admin_bootstrap_password, cfg.zarinpal_merchant_id, cfg.stars_supplier_api_key):
        if secret and len(secret) >= 6:
            out = out.replace(secret, "***")
    return out


_NUM_TRANS = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")


def parse_int(text) -> int | None:
    """'۱٬۵۰۰' / '1,500' / ' 1500 ' -> 1500. None when it is not a plain positive whole number."""
    cleaned = str(text or "").translate(_NUM_TRANS)
    for ch in (",", "٬", "،", " ", "_"):
        cleaned = cleaned.replace(ch, "")
    if not (cleaned.isascii() and cleaned.isdigit() and len(cleaned) <= 12):
        return None
    value = int(cleaned)
    return value if value > 0 else None
