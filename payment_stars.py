"""Telegram Stars (XTR) payments through the official Bot API.

Rules enforced here
  * the order exists (PENDING_PAYMENT) BEFORE the invoice is sent
  * pre-checkout validates order / owner / amount / expiry
  * successful_payment is re-validated; the order becomes PAID only through an atomic transition
  * telegram_payment_charge_id is stored as the unique payment id ("stars:<charge_id>")
  * duplicates (replayed updates) are ignored; delivery failures never ask for money again
"""
from __future__ import annotations

import logging
from constants import OrderStatus as S
from db import get_db
from payment_base import PaymentInit, PaymentProvider, PaymentResult
import notifier, orders
import settings as st
from util import as_aware, utcnow

log = logging.getLogger(__name__)
CURRENCY = "XTR"


class StarsProvider(PaymentProvider):
    key = "stars"
    display_name = "پرداخت با Telegram Stars"

    async def is_enabled(self) -> bool:
        return await st.get_bool("payment_stars_enabled")

    async def create_payment(self, order: dict) -> PaymentInit:
        if order["status"] != S.PENDING_PAYMENT:
            raise ValueError("order is not payable")
        if as_aware(order["expires_at"]) <= utcnow():
            raise ValueError("order expired")
        amount = order.get("stars_amount")
        if not amount or amount < 1:
            raise ValueError("stars amount not available")
        return PaymentInit("telegram_invoice", {
            "title": order["product_name"][:32],
            "description": f"سفارش {order['public_id'][:8]} - تعداد: {order['quantity']}"[:255],
            "payload": order["public_id"],       # 32 hex chars (<= 128 bytes limit)
            "currency": CURRENCY,
            "amount": int(amount),
        })

    async def pre_checkout(self, user_id: int, payload: str, currency: str, total: int) -> tuple[bool, str]:
        order = await orders.get_order(payload)
        if not order:
            return False, "سفارش پیدا نشد."
        if order["user_id"] != user_id:
            return False, "این سفارش متعلق به شما نیست."
        if order.get("payment_method") != "stars":
            return False, "این سفارش با روش دیگری باید پرداخت شود."
        if order["status"] != S.PENDING_PAYMENT:
            return False, "این سفارش دیگر قابل پرداخت نیست."
        if as_aware(order["expires_at"]) <= utcnow():
            return False, "مهلت پرداخت این سفارش تمام شده است. لطفاً دوباره سفارش دهید."
        if currency != CURRENCY or total != order.get("stars_amount"):
            return False, "مبلغ پرداخت با سفارش مطابقت ندارد."
        await get_db().orders.update_one({"public_id": payload}, {"$set": {"pre_checkout_ok_at": utcnow()}})
        return True, ""

    async def handle_successful_payment(self, user_id: int, payload: str, currency: str, total: int,
                                        charge_id: str, provider_charge_id: str = "") -> PaymentResult:
        db = get_db()
        pay_id = f"stars:{charge_id}"
        order = await orders.get_order(payload)
        base = {
            "provider": "stars", "telegram_payment_charge_id": charge_id,
            "provider_payment_charge_id": provider_charge_id, "user_id": user_id,
            "currency": currency, "amount": total, "order_id": payload, "created_at": utcnow(),
        }
        if not order:
            await db.payments.update_one({"_id": pay_id}, {"$setOnInsert": {**base, "status": "ORPHAN"}}, upsert=True)
            await notifier.send_admins(f"پرداخت Stars بدون سفارش معتبر: charge={charge_id} user={user_id} amount={total}")
            return PaymentResult("UNKNOWN_ORDER")

        mismatch = (currency != CURRENCY or total != order.get("stars_amount") or user_id != order["user_id"])
        if mismatch:
            await db.payments.update_one({"_id": pay_id}, {"$setOnInsert": {**base, "status": "MISMATCH"}}, upsert=True)
            await orders.transition(payload, S.FAILED, from_states=[S.PENDING_PAYMENT], note="payment mismatch")
            await notifier.send_admins(
                f"عدم تطابق پرداخت Stars: order={payload} expected={order.get('stars_amount')} got={total} {currency} charge={charge_id}")
            return PaymentResult("MISMATCH", order)

        if order.get("payment_method") != "stars":      # money arrived for an order that expects another method
            await db.payments.update_one({"_id": pay_id}, {"$setOnInsert": {**base, "status": "NEEDS_REFUND"}}, upsert=True)
            await notifier.send_admins(f"پرداخت Stars برای سفارشی با روش پرداخت دیگر: order={payload} charge={charge_id} — نیاز به بازپرداخت")
            return PaymentResult("ORDER_CLOSED", order)

        # idempotent payment record (unique _id = charge id)
        await db.payments.update_one({"_id": pay_id}, {"$setOnInsert": {**base, "status": "RECEIVED"}}, upsert=True)
        updated = await orders.transition(
            payload, S.PAID, from_states=[S.PENDING_PAYMENT],
            set_fields={"paid_at": utcnow(), "paid_charge_id": charge_id, "payment_status": "PAID"},
            note="telegram payment verified",
        )
        if updated:
            await db.payments.update_one({"_id": pay_id}, {"$set": {"status": "SUCCEEDED", "verified_at": utcnow()}})
            return PaymentResult("OK", updated)
        fresh = await orders.get_order(payload)
        if fresh and fresh.get("paid_charge_id") == charge_id:
            return PaymentResult("DUPLICATE", fresh)
        await db.payments.update_one({"_id": pay_id}, {"$set": {"status": "NEEDS_REFUND"}})
        await notifier.send_admins(f"پرداخت Stars برای سفارش بسته‌شده: order={payload} charge={charge_id} — نیاز به بازپرداخت")
        return PaymentResult("ORDER_CLOSED", fresh)

    async def refund(self, order: dict, *, bot=None, actor: str = "system") -> bool:
        """Refund Stars via Bot API (refundStarPayment). Atomic: only one refund can start."""
        charge = order.get("paid_charge_id")
        if order.get("payment_method") != "stars" or not charge or bot is None:
            return False
        started = await orders.transition(
            order["public_id"], S.REFUND_PENDING,
            from_states=[S.PAID, S.PROCESSING, S.FAILED, S.FULFILLED], note=f"refund by {actor}")
        if not started:
            return False
        try:
            await bot.refund_star_payment(user_id=order["user_id"], telegram_payment_charge_id=charge)
        except Exception as exc:
            log.exception("refund failed")
            await orders.transition(order["public_id"], S.FAILED, from_states=[S.REFUND_PENDING],
                                    set_fields={"fulfillment_error": f"refund failed: {exc}"}, note="refund failed")
            return False
        await orders.transition(order["public_id"], S.REFUNDED, from_states=[S.REFUND_PENDING],
                                set_fields={"payment_status": "REFUNDED"}, note="refunded")
        await get_db().payments.update_one({"_id": f"stars:{charge}"}, {"$set": {"status": "REFUNDED", "refunded_at": utcnow()}})
        return True
