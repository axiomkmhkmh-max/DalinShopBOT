"""Runtime settings stored in MongoDB (editable from the admin dashboard)."""
from __future__ import annotations

import config as _config
from db import get_db
from money import MoneyError, ROUNDING_MODES, to_decimal, validate_profit
from util import utcnow

# NOTE: no live rate / price is hardcoded here. Missing values mean "not configured".
DEFAULTS: dict[str, str | None] = {
    "profit_toman": "0",
    "rounding_step": "1000",
    "rounding_mode": "up",
    "refresh_interval_minutes": "30",
    "max_age_minutes": "120",               # automatic rates / Telegram data older than this stop sales
    "manual_rate_max_age_minutes": "1440",  # a rate typed by hand stops sales after this (default 24h)
    "order_ttl_minutes": "15",
    "stars_auto_min": "50",                 # Stars orders >= this many Stars are delivered automatically IF a sender exists
    "auto_refresh_enabled": "true",         # scheduled refresh of rates + Telegram prices/availability
    "auto_delivery_enabled": "true",        # false => Premium/Gift orders wait for manual delivery
    "stars_usd_value": None,       # value of 1 Star in USD for CUSTOMER pricing (admin must set)
    "stars_cost_usd": None,        # what 1 Star costs YOU in USD (base cost of Stars/Premium/Gift products)
    "stars_payout_usd_value": None,  # what the seller receives per Star (informational margin check)
    "payment_stars_enabled": "true",
    "wallet_payment_enabled": "false",
    "referral_reward_toman": "0",
    # wallet top-up through Zarinpal (OFF by default: see README, Telegram's rules for digital goods)
    "zarinpal_topup_enabled": "false",
    "topup_min_toman": "10000",
    "topup_max_toman": "5000000",
    "topup_presets": "50000,100000,200000,500000",
    # custom Stars amount (typed by the customer)
    "custom_stars_enabled": "false",
    "custom_stars_min": "50",
    "custom_stars_max": "10000",
    # wallet-paid order whose delivery DEFINITELY failed before anything was sent: refund to the wallet automatically
    "wallet_auto_refund_on_fail": "false",
}


# An Environment Variable (when set) wins over the dashboard value, so a beginner can switch features without touching code.
ENV_OVERRIDES = {"zarinpal_topup_enabled": "zarinpal_enabled", "wallet_payment_enabled": "wallet_payments_enabled",
                 "custom_stars_enabled": "custom_stars_enabled"}


def env_override(key: str) -> str | None:
    attr = ENV_OVERRIDES.get(key)
    val = getattr(_config.get_config(), attr, None) if attr else None
    return None if val is None else ("true" if val else "false")


async def get_setting(key: str) -> str | None:
    forced = env_override(key)
    if forced is not None:
        return forced
    doc = await get_db().settings.find_one({"key": key})
    if doc is not None:
        return doc["value"]
    return DEFAULTS.get(key)


def _validate(key: str, value: str) -> str:
    value = str(value).strip()
    if key == "profit_toman":
        return str(validate_profit(value))           # raises if outside 0..7000
    if key == "rounding_mode":
        if value not in ROUNDING_MODES:
            raise MoneyError("invalid rounding mode")
    elif key in ("rounding_step", "refresh_interval_minutes", "max_age_minutes", "manual_rate_max_age_minutes",
                 "order_ttl_minutes", "stars_auto_min", "topup_min_toman", "topup_max_toman", "custom_stars_min",
                 "custom_stars_max"):
        if not value.isdigit() or int(value) < 1:
            raise MoneyError(f"{key} must be a positive integer")
        if key == "refresh_interval_minutes" and int(value) < 5:
            raise MoneyError("refresh interval must be >= 5 minutes (avoid API abuse)")
    elif key in ("stars_usd_value", "stars_payout_usd_value", "stars_cost_usd"):
        if to_decimal(value) <= 0:
            raise MoneyError("value must be positive")
    elif key == "referral_reward_toman":
        if not value.isdigit():
            raise MoneyError("reward must be a whole number")
    elif key == "topup_presets":
        parts = [p.strip() for p in value.replace("،", ",").replace("٬", ",").split(",") if p.strip()]
        if not parts or len(parts) > 8 or not all(p.isascii() and p.isdigit() and 0 < int(p) <= 10**9 for p in parts):
            raise MoneyError("presets must be up to 8 whole numbers separated by commas")
        value = ",".join(str(int(p)) for p in parts)
    elif key in ("payment_stars_enabled", "wallet_payment_enabled", "auto_refresh_enabled", "auto_delivery_enabled",
                 "zarinpal_topup_enabled", "custom_stars_enabled", "wallet_auto_refund_on_fail"):
        value = "true" if value.lower() in ("1", "true", "yes", "on") else "false"
    elif key not in DEFAULTS:
        raise MoneyError("unknown setting")
    return value


async def set_setting(key: str, value: str) -> str:
    value = _validate(key, value)
    await get_db().settings.update_one(
        {"key": key}, {"$set": {"value": value, "updated_at": utcnow()}}, upsert=True
    )
    return value


async def get_int(key: str) -> int:
    return int(await get_setting(key) or DEFAULTS[key])


async def get_bool(key: str) -> bool:
    return (await get_setting(key) or "false") == "true"


async def all_settings() -> dict[str, str | None]:
    return {k: await get_setting(k) for k in DEFAULTS}
