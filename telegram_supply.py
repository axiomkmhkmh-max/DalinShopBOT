"""Telegram itself as the supplier (official Bot API only — no user-account login, no MTProto session).

What the Bot API really offers (checked against aiogram 3.31 / Bot API docs):
  * getAvailableGifts      -> list of gifts with their Star price and, for limited gifts, remaining_count
  * sendGift               -> the BOT sends a gift to a user, paid from the BOT's Star balance
  * giftPremiumSubscription-> the BOT gifts Telegram Premium (3/6/12 months = 1000/1500/2500 Stars), paid from
                              the bot's Star balance
  * getMyStarBalance       -> the bot's Star balance (it is filled by the Stars your customers pay)
What it does NOT offer: buying or sending plain "Stars" to a user from your personal account. Stars products are
therefore delivered manually (the order waits in the dashboard until you confirm delivery).

Safety rules for automatic delivery
  * every send is preceded by a durable "SENDING" marker; after a crash or network timeout (Telegram may or may
    not have executed the call) the order is NEVER re-sent automatically — it goes to manual review.
  * multi-unit orders remember how many units were already sent, so a retry never duplicates a unit.
"""
from __future__ import annotations

import logging

from aiogram.exceptions import (
    TelegramBadRequest, TelegramForbiddenError, TelegramNotFound, TelegramRetryAfter, TelegramUnauthorizedError,
)

import catalog
from db import get_db
import notifier
import pricing_engine as engine
import settings as st
from supplier_base import Supplier, SupplierRequest, SupplierResult
from util import redact, utcnow

log = logging.getLogger(__name__)

# Fixed by the Bot API (giftPremiumSubscription): months -> Stars that must be paid.
PREMIUM_STARS: dict[int, int] = {3: 1000, 6: 1500, 12: 2500}
AUTO_DELIVERIES = catalog.AUTO_DELIVERIES
needs_supply = catalog.needs_supply

# Telegram answered "no" => nothing was executed => safe to retry later.
_DEFINITE_FAILURE = (TelegramBadRequest, TelegramForbiddenError, TelegramNotFound, TelegramRetryAfter,
                     TelegramUnauthorizedError)


# ------------------------------------------------------------------ sync (prices + availability + balance)
def _supply_for(product: dict, gifts: dict | None, now) -> dict | None:
    """New `supply` document for a product, or None when Telegram could not be asked (keep old data, it ages out)."""
    if product.get("kind") == "premium" or product.get("delivery") == "auto_premium":
        try:
            cost = PREMIUM_STARS.get(int(product.get("amount") or 0))
        except (TypeError, ValueError):
            cost = None
        if not cost:
            return {"checked_at": now, "available": False, "remaining": None, "stars_cost": None,
                    "error": "Premium months must be 3, 6 or 12"}
        return {"checked_at": now, "available": True, "remaining": None, "stars_cost": cost, "error": None}
    if gifts is None:
        return None
    g = gifts.get(product.get("gift_id") or "")
    if g is None:
        return {"checked_at": now, "available": False, "remaining": 0, "stars_cost": None,
                "error": "this gift is not in Telegram's available list"}
    remaining = g.remaining_count
    return {"checked_at": now, "available": remaining is None or remaining > 0, "remaining": remaining,
            "stars_cost": int(g.star_count), "error": None}


async def sync_supply(bot) -> dict:
    """Ask Telegram for gift prices/availability and the bot's Star balance; store them on the products.
    Never raises. The summary is kept in settings key `_supply_status` for the dashboard."""
    db = get_db()
    now = utcnow()
    status = {"checked_at": now, "ok": True, "error": None, "gifts_count": None,
              "bot_balance": None, "balance_error": None}
    gifts = None
    if bot is None:
        status.update(ok=False, error="bot is not running (BOT_TOKEN missing)")
    else:
        try:
            gifts = {g.id: g for g in (await bot.get_available_gifts()).gifts}
            status["gifts_count"] = len(gifts)
        except Exception as exc:
            status.update(ok=False, error=redact(f"getAvailableGifts failed: {type(exc).__name__}: {exc}"))
        try:
            status["bot_balance"] = int((await bot.get_my_star_balance()).amount)
        except Exception as exc:
            status["balance_error"] = redact(f"getMyStarBalance failed: {type(exc).__name__}: {exc}")
    async for p in db.products.find({}):
        if not needs_supply(p):
            continue
        supply = _supply_for(p, gifts, now) if bot is not None else None
        if supply is not None:
            await db.products.update_one({"public_id": p["public_id"]}, {"$set": {"supply": supply}})
        elif status["error"]:
            await db.products.update_one({"public_id": p["public_id"]}, {"$set": {"supply.error": status["error"]}})
    await db.settings.update_one({"key": "_supply_status"}, {"$set": {"value": status, "updated_at": now}}, upsert=True)
    return status


async def get_supply_status() -> dict:
    doc = await get_db().settings.find_one({"key": "_supply_status"})
    return (doc or {}).get("value") or {}


# ------------------------------------------------------------------ catalog helpers (admin buttons)
async def _category_id(name: str) -> str:
    db = get_db()
    cat = await db.categories.find_one({"name": name})
    return cat["public_id"] if cat else await catalog.save_category(name)


async def import_gifts(bot) -> int:
    """Create one product per Telegram gift that is not in the shop yet. New products are DISABLED:
    enable the ones you want to sell. Returns how many were created."""
    gifts = (await bot.get_available_gifts()).gifts
    db = get_db()
    cat_id = await _category_id("Telegram Gifts")
    created = 0
    for g in gifts:
        if await db.products.find_one({"gift_id": g.id}):
            continue
        emoji = getattr(g.sticker, "emoji", None) or "🎁"
        await catalog.save_product({
            "name": f"گیفت {emoji} ({g.star_count} Stars)", "description": "گیفت تلگرام برای حساب شما",
            "kind": "gift", "category_id": cat_id, "enabled": False, "amount": None, "gift_id": g.id,
            "delivery": "auto_gift", "supplier_key": "telegram", "stock": None,
            "pricing": {"source": "telegram"}, "digital_in_telegram": True})
        created += 1
    await sync_supply(bot)
    await engine.refresh_all(None, fetch=False)
    return created


async def create_premium_products() -> int:
    """Create the 3/6/12-month Premium packages that do not exist yet (DISABLED until you enable them)."""
    db = get_db()
    cat_id = await _category_id("Telegram Premium")
    created = 0
    for months in PREMIUM_STARS:
        if await db.products.find_one({"kind": "premium", "amount": months, "delivery": "auto_premium"}):
            continue
        await catalog.save_product({
            "name": f"Telegram Premium {months} ماهه", "description": f"اشتراک {months} ماهه تلگرام پریمیوم",
            "kind": "premium", "category_id": cat_id, "enabled": False, "amount": months, "gift_id": None,
            "delivery": "auto_premium", "supplier_key": "telegram", "stock": None,
            "pricing": {"source": "telegram"}, "digital_in_telegram": True})
        created += 1
    return created


# ------------------------------------------------------------------ automatic delivery
class TelegramSupplier(Supplier):
    key = "telegram"

    async def place_order(self, req: SupplierRequest) -> SupplierResult:
        p = req.product
        delivery = p.get("delivery")
        if delivery not in AUTO_DELIVERIES:
            return SupplierResult("FAILED", "this product has no automatic delivery")
        if not await st.get_bool("auto_delivery_enabled"):
            return SupplierResult("PENDING", "automatic delivery is switched off in the dashboard: deliver manually")
        bot = notifier.get_bot()
        if bot is None:
            return SupplierResult("FAILED", "bot is not running")
        if delivery == "auto_gift" and not p.get("gift_id"):
            return SupplierResult("FAILED", "product has no gift_id")
        if delivery == "auto_premium" and int(p.get("amount") or 0) not in PREMIUM_STARS:
            return SupplierResult("FAILED", "Premium months must be 3, 6 or 12")

        db = get_db()
        prior = await db.supplier_orders.find_one({"order_id": req.order_id}) or {}
        if prior.get("send_state") == "SENDING":
            return SupplierResult(
                "PENDING",
                "the previous send attempt has an UNKNOWN result: check in Telegram whether the user received it, "
                "then press «تحویل شد» or refund. It is never re-sent automatically.")
        done = int(prior.get("units_done") or 0)
        for unit in range(done, req.quantity):
            await db.supplier_orders.update_one(
                {"order_id": req.order_id},
                {"$set": {"send_state": "SENDING", "send_unit": unit, "updated_at": utcnow()},
                 "$setOnInsert": {"created_at": utcnow(), "supplier_key": "telegram", "status": "SENDING"}},
                upsert=True)
            try:
                if delivery == "auto_gift":
                    await bot.send_gift(gift_id=p["gift_id"], user_id=req.user_id)
                else:
                    months = int(p["amount"])
                    await bot.gift_premium_subscription(
                        user_id=req.user_id, month_count=months, star_count=PREMIUM_STARS[months])
            except _DEFINITE_FAILURE as exc:
                await db.supplier_orders.update_one({"order_id": req.order_id}, {"$set": {"send_state": "IDLE"}})
                return SupplierResult(
                    "FAILED", redact(f"Telegram refused (sent {unit} of {req.quantity}): {type(exc).__name__}: {exc}"))
            except Exception as exc:  # timeout / network / 5xx: Telegram may have executed it
                log.exception("unknown outcome while sending order %s", req.order_id)
                return SupplierResult(
                    "PENDING",
                    redact(f"UNKNOWN result ({type(exc).__name__}) while sending: check the user in Telegram, then "
                           f"press «تحویل شد» or refund. It is never re-sent automatically."))
            await db.supplier_orders.update_one(
                {"order_id": req.order_id}, {"$set": {"send_state": "IDLE"}, "$inc": {"units_done": 1}})
        return SupplierResult("DELIVERED", "sent via Telegram Bot API", external_ref=f"tg-{req.order_id[:8]}")
