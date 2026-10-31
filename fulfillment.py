"""Fulfillment: only runs for PAID orders; claim-by-transition prevents duplicate fulfillment."""
from __future__ import annotations

import logging

from constants import OrderStatus as S
from db import get_db
import notifier, orders, wallet
import settings as st
from supplier_base import SupplierRequest, get_supplier
from util import redact, utcnow

log = logging.getLogger(__name__)

MSG_DELIVERED = "✅ سفارش شما تحویل داده شد.\nشماره سفارش: {oid}"
MSG_PENDING = "✅ پرداخت شما تأیید شد و سفارش در حال پردازش است.\nشماره سفارش: {oid}"
MSG_FAILED = (
    "پرداخت شما با موفقیت ثبت شد، اما تحویل سفارش با تأخیر مواجه شد.\n"
    "نیازی به پرداخت مجدد نیست؛ همکاران ما موضوع را بررسی می‌کنند.\nشماره سفارش: {oid}"
)


async def on_order_paid(order: dict) -> str:
    """Called exactly once per successful payment transition. Never raises."""
    try:
        await maybe_reward_referral(order)
    except Exception:
        log.exception("referral reward failed")
    try:
        return await fulfill_order(order["public_id"])
    except Exception:
        log.exception("fulfillment crashed for %s", order["public_id"])
        await orders.transition(order["public_id"], S.FAILED, from_states=[S.PROCESSING],
                                set_fields={"fulfillment_status": "FAILED", "fulfillment_error": "internal error"})
        await notifier.send_user(order["user_id"], MSG_FAILED.format(oid=order["public_id"][:8]))
        await notifier.send_admins(f"Fulfillment crashed for order {order['public_id']}")
        return "FAILED"


async def fulfill_order(order_id: str) -> str:
    db = get_db()
    order = await orders.get_order(order_id)
    if not order or not order.get("paid_at"):
        return "SKIPPED"                       # never fulfill before verified payment
    claimed = await orders.transition(
        order_id, S.PROCESSING, from_states=[S.PAID, S.FAILED],
        set_fields={"fulfillment_status": "PROCESSING", "fulfillment_error": None}, note="fulfillment claimed",
    )
    if claimed is None:
        return "SKIPPED"                       # someone else claimed it (duplicate protection)
    product = await db.products.find_one({"public_id": order["product_id"]}) or {}
    product = {**product, **{k: v for k, v in (order.get("delivery_spec") or {}).items() if v is not None}}
    oid_short = order_id[:8]

    prior = await db.supplier_orders.find_one({"order_id": order_id})
    if prior and prior.get("status") == "DELIVERED":
        await _finish(order, "")
        return "DELIVERED"

    try:
        key = product.get("supplier_key")
        if product.get("kind") == "stars" and key in (None, "manual", "stars"):
            key = "stars"        # plain Stars: automatic from the minimum IF a real sender exists, else manual
        supplier = await get_supplier(key)
        result = await supplier.place_order(
            SupplierRequest(order_id, product, order["quantity"], order["user_id"])
        )
        status, message, ext, text = result.status, result.message, result.external_ref, result.delivery_text
    except Exception as exc:
        status, message, ext, text = "FAILED", redact(f"{type(exc).__name__}: {exc}"), "", ""

    await db.supplier_orders.update_one(
        {"order_id": order_id},
        {"$setOnInsert": {"created_at": utcnow(), "supplier_key": product.get("supplier_key") or "manual"},
         "$set": {"status": status, "external_ref": ext, "message": message, "updated_at": utcnow()},
         "$inc": {"attempts": 1},
         "$push": {"events": {"at": utcnow(), "status": status, "message": message}}},
        upsert=True,
    )

    if status == "DELIVERED":
        await _finish(order, text)
        return "DELIVERED"
    if status == "PENDING":
        await db.orders.update_one({"public_id": order_id}, {"$set": {"fulfillment_status": "MANUAL_PENDING", "manual_reason": message}})
        await notifier.send_user(order["user_id"], MSG_PENDING.format(oid=oid_short))
        await notifier.send_admins(await _manual_alert(order, product, message))
        return "PENDING"
    await orders.transition(order_id, S.FAILED, from_states=[S.PROCESSING],
                            set_fields={"fulfillment_status": "FAILED", "fulfillment_error": message}, note=message)
    await notifier.send_user(order["user_id"], MSG_FAILED.format(oid=oid_short))     # no new charge asked
    await notifier.send_admins(f"تحویل سفارش {order_id} ناموفق بود: {message}")
    return "FAILED"


async def _manual_alert(order: dict, product: dict, message: str) -> str:
    """Everything the admin needs to deliver by hand: what, how many, to whom."""
    user = await get_db().users.find_one({"telegram_id": order["user_id"]}) or {}
    who = f"@{user['username']} " if user.get("username") else ""
    what = product.get("name") or order.get("product_name")
    amount = f" ({product['amount']})" if product.get("amount") else ""
    text = (f"🔔 تحویل دستی لازم است\nسفارش: {order['public_id']}\nمحصول: {what}{amount} × {order['quantity']}\n"
            f"گیرنده: {who}{order['user_id']}\n(پنل ادمین > سفارش‌ها > «تحویل شد»)")
    if message and message != "manual fulfillment required":
        text += f"\nعلت: {message}"
    return text


async def _finish(order: dict, delivery_text: str) -> None:
    done = await orders.transition(
        order["public_id"], S.FULFILLED, from_states=[S.PROCESSING],
        set_fields={"fulfillment_status": "DONE", "completed_at": utcnow()}, note="delivered",
    )
    if done:
        text = MSG_DELIVERED.format(oid=order["public_id"][:8])
        if delivery_text:
            text += f"\n\n{delivery_text}"
        await notifier.send_user(order["user_id"], text)


async def complete_manual(order_id: str, actor: str, delivery_text: str = "") -> bool:
    """Admin confirms a manual delivery."""
    db = get_db()
    order = await orders.get_order(order_id)
    if not order or order["status"] != S.PROCESSING:
        return False
    await db.supplier_orders.update_one(
        {"order_id": order_id},
        {"$set": {"status": "DELIVERED", "send_state": "IDLE", "updated_at": utcnow(), "delivered_by": actor}},
        upsert=True)
    await _finish(order, delivery_text)
    return True


async def maybe_reward_referral(order: dict) -> None:
    """Reward only after a VERIFIED paid order, once per referred user."""
    reward = int(await st.get_setting("referral_reward_toman") or 0)
    if reward <= 0:
        return
    db = get_db()
    user = await db.users.find_one({"telegram_id": order["user_id"]})
    if not user or not user.get("referred_by") or user.get("referral_rewarded"):
        return
    try:
        await wallet.credit(user["referred_by"], reward, idem_key=f"referral:{user['telegram_id']}",
                            kind="REFERRAL", ref=order["public_id"], actor="system")
    except wallet.DuplicateTransaction:
        pass
    await db.users.update_one({"telegram_id": user["telegram_id"]}, {"$set": {"referral_rewarded": True}})
