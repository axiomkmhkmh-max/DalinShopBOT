"""Delivery of plain Telegram Stars (rule: orders of >= `stars_auto_min` Stars (default 50) are automatic IF possible).

HONEST STATUS (checked against core.telegram.org docs):
  * Bot API has NO method that sends Stars to a user. The bot's Star balance can only pay for gifts/Premium
    (see telegram_supply.py) or refund a user's OWN payment.
  * MTProto only offers "buy Stars for another user" as a STORE payment (payments.getStarsGiftOptions +
    inputStorePaymentStarsGift), not a transfer from the Star balance of your personal account. It would also need a
    logged-in user Session, which this project deliberately never stores.
=> No supported way exists today, so NO sender is registered in production and every Stars order stays safely in
   "needs manual delivery". There is no fake/simulated sending anywhere in this file.

The machinery below is the safe place to plug an official method in later: implement StarsSender and call
register_sender(...). Everything else (threshold, balance check before payment and before sending, never re-sending
after an unknown result, no duplicate sends, marking the order for admin review) already works and is tested with a
test-only sender.
Do not confuse three balances:  the BOT's Stars (telegram_supply)  /  your PERSONAL account's Stars  /  the
`balance()` of whichever StarsSender is registered (the account that would really send).
"""
from __future__ import annotations

import logging
from abc import ABC, abstractmethod

from db import get_db
import settings as st
from config import get_config
from supplier_base import Supplier, SupplierRequest, SupplierResult
from util import redact, utcnow

log = logging.getLogger(__name__)


class StarsSendRefused(Exception):
    """The sender is SURE nothing was sent (safe to retry). Any other exception = unknown result."""


class InsufficientSenderBalance(Exception):
    pass


class StarsSender(ABC):
    name = "abstract"

    @abstractmethod
    async def is_available(self) -> tuple[bool, str]: ...

    @abstractmethod
    async def balance(self) -> int:
        """Stars balance of the account that would really send (NOT the bot's balance)."""

    @abstractmethod
    async def send_stars(self, user_id: int, stars: int, idempotency_key: str, *, target_username: str | None = None) -> None:
        """`user_id` is the BUYER; `target_username` (without @) is the destination account the customer typed."""


_sender: StarsSender | None = None


def register_sender(sender: StarsSender | None) -> None:
    global _sender
    _sender = sender


def get_sender() -> StarsSender | None:
    return _sender


def stars_total(product: dict, quantity: int) -> int:
    return int(product.get("amount") or 0) * int(quantity)


async def auto_plan(product: dict, quantity: int) -> tuple[bool, str]:
    """(automatic delivery possible?, reason in Persian when not)."""
    total = stars_total(product, quantity)
    if not get_config().auto_stars_delivery:
        return False, "تحویل خودکار Stars خاموش است (AUTO_STARS_DELIVERY=false)؛ تحویل دستی"
    minimum = await st.get_int("stars_auto_min")
    if total < minimum:
        return False, f"کمتر از {minimum} Stars؛ تحویل دستی"
    sender = get_sender()
    if sender is None:
        return False, "روش رسمی ارسال خودکار Stars وجود ندارد؛ تحویل دستی"
    ok, why = await sender.is_available()
    if not ok:
        return False, f"ارسال خودکار در دسترس نیست ({why}); تحویل دستی"
    return True, ""


async def precheck(product: dict, quantity: int) -> None:
    """Before the customer pays: if this order would be sent automatically, the sender must hold enough Stars."""
    if product.get("kind") != "stars":
        return
    auto, _ = await auto_plan(product, quantity)
    if not auto:
        if not get_config().manual_stars_delivery:
            raise InsufficientSenderBalance("تحویل Stars فعلاً غیرفعال است (MANUAL_STARS_DELIVERY=false)")
        return                                   # manual delivery: nothing to check here
    try:
        bal = await get_sender().balance()
    except Exception:
        return                                   # unknown balance: the send step re-checks and falls back safely
    if bal < stars_total(product, quantity):
        raise InsufficientSenderBalance("موجودی ارسال خودکار Stars کافی نیست؛ موقتاً قابل خرید نیست")


class StarsSupplier(Supplier):
    key = "stars"

    async def place_order(self, req: SupplierRequest) -> SupplierResult:
        p = req.product
        total = stars_total(p, req.quantity)
        if total < 1:
            return SupplierResult("FAILED", "product has no Stars amount")
        auto, why = await auto_plan(p, req.quantity)
        if not auto:
            return SupplierResult("PENDING", why)
        sender = get_sender()
        db = get_db()
        prior = await db.supplier_orders.find_one({"order_id": req.order_id}) or {}
        if prior.get("send_state") == "SENDING":
            return SupplierResult(
                "PENDING", "نتیجه‌ی ارسال قبلی نامشخص است: در تلگرام بررسی کنید و سپس «تحویل شد» یا بازپرداخت بزنید. "
                           "هرگز خودکار دوباره ارسال نمی‌شود.")
        if int(prior.get("units_done") or 0) >= 1:
            return SupplierResult("DELIVERED", "already sent")              # never a second send
        try:
            bal = await sender.balance()
        except Exception:
            return SupplierResult("PENDING", "موجودی ارسال‌کننده قابل خواندن نیست؛ تحویل دستی")
        if bal < total:
            return SupplierResult("FAILED", f"موجودی ارسال‌کننده ({bal}) کمتر از {total} Stars است؛ شارژ کنید و تلاش مجدد بزنید")
        await db.supplier_orders.update_one(
            {"order_id": req.order_id},
            {"$set": {"send_state": "SENDING", "updated_at": utcnow()},
             "$setOnInsert": {"created_at": utcnow(), "supplier_key": "stars", "status": "SENDING"}},
            upsert=True)
        try:
            extra = {"target_username": req.target_username} if req.target_username else {}
            await sender.send_stars(req.user_id, total, f"order:{req.order_id}", **extra)
        except StarsSendRefused as exc:
            await db.supplier_orders.update_one({"order_id": req.order_id}, {"$set": {"send_state": "IDLE"}})
            return SupplierResult("FAILED", redact(f"ارسال رد شد: {exc}"))
        except Exception as exc:
            log.exception("unknown outcome sending Stars for %s", req.order_id)
            return SupplierResult(
                "PENDING", redact(f"نتیجه‌ی ارسال نامشخص ({type(exc).__name__}): در تلگرام بررسی کنید و سپس «تحویل شد» "
                                  "یا بازپرداخت بزنید. هرگز خودکار دوباره ارسال نمی‌شود."))
        await db.supplier_orders.update_one(
            {"order_id": req.order_id}, {"$set": {"send_state": "IDLE"}, "$inc": {"units_done": 1}})
        return SupplierResult("DELIVERED", f"sent {total} Stars", external_ref=f"stars-{req.order_id[:8]}")
