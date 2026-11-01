"""Orders: price snapshot at creation, strict state machine, atomic transitions."""
from __future__ import annotations

from datetime import timedelta

from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError

from constants import ALL_STATUSES, OrderStatus as S, can_transition, sources_for
from db import get_db
import access
import settings as st
from catalog import delivery_spec, get_product, purchasable
import pricing_engine as engine
import stars_delivery
from stars_targets import InvalidUsername, normalize_username
from util import as_aware, new_id, utcnow


class OrderError(Exception):
    pass


async def _custom_quote(quantity: int) -> dict:
    if not await st.get_bool("custom_stars_enabled"):
        raise OrderError("خرید با تعداد دلخواه فعال نیست")
    lo, hi = await st.get_int("custom_stars_min"), await st.get_int("custom_stars_max")
    if not lo <= quantity <= hi:
        raise OrderError(f"تعداد Stars باید بین {lo} و {hi} باشد")
    try:
        return await engine.custom_stars_quote(quantity)
    except engine.PriceUnavailable:
        raise OrderError("قیمت در حال به‌روزرسانی است؛ موقتاً قابل خرید نیست")


async def create_order(user_id: int, product_id: str, quantity: int, payment_method: str = "stars", *,
                       target_username: str | None = None) -> dict:
    """Create (or reuse the still-open) order. The price is FROZEN here and never recomputed.

    target_username: destination @username of a Stars order (format-validated, see stars_targets.py). It is part of the
    duplicate-order key, so the same user ordering for two different accounts gets two different orders."""
    allowed, why = access.check_payment_access(user_id)
    if not allowed:
        raise OrderError(why)
    if quantity < 1:
        raise OrderError("invalid quantity")
    product = await get_product(product_id)
    if not product:
        raise OrderError("product not found")
    target = None
    if target_username and product.get("kind") == "stars":
        try:
            target = normalize_username(target_username)
        except InvalidUsername as exc:
            raise OrderError(str(exc))
    custom = bool(product.get("custom_amount"))
    if custom:
        quote = await _custom_quote(quantity)
    else:
        if quantity > 20:
            raise OrderError("invalid quantity")
        ok, reason, quote = await purchasable(product)
        if not ok:
            raise OrderError(reason)
    if custom:
        _tot = int(quote["final_toman"])
    else:
        _tot = int(quote["final_toman"]) * quantity
    if product.get("kind") == "stars":
        import supplier_tracking                              # lazy: supplier_tracking imports orders
        problem = await supplier_tracking.guard_new_order(stars_delivery.stars_total(product, quantity), _tot)
        if problem:
            raise OrderError(problem)
    try:
        await stars_delivery.precheck(product, quantity)      # sender balance, BEFORE the customer pays
    except stars_delivery.InsufficientSenderBalance as exc:
        raise OrderError(str(exc))
    db = get_db()
    open_key = f"{user_id}:{product_id}:{quantity}:{payment_method}" + (f":{target.lower()}" if target else "")
    now = utcnow()

    existing = await db.orders.find_one({"open_key": open_key})
    if existing:
        if as_aware(existing["expires_at"]) > now:
            return existing            # duplicate order prevention: reuse the open order (price stays locked)
        await transition(existing["public_id"], S.CANCELLED, note="expired")

    if custom:
        total_toman = int(quote["final_toman"])
        unit = -(-total_toman // quantity)                       # informational; the total is what is charged
        stars_total = int(quote["stars_amount"])                 # already the TOTAL for the typed amount
    else:
        unit = int(quote["final_toman"])
        total_toman = unit * quantity
        # Stars are computed PER UNIT (kept inside the 7000 Toman profit cap) and multiplied, so the cap holds per item.
        stars_total = int(quote["stars_amount"]) * quantity if quote.get("stars_amount") else None

    # Reserve stock (only products with a stock number; empty = unlimited). Released again if the order is
    # cancelled / expires / is refunded before delivery (see transition()).
    reserved = 0
    if product.get("stock") is not None:
        res = await db.products.update_one({"public_id": product_id, "stock": {"$gte": quantity}},
                                           {"$inc": {"stock": -quantity}})
        if res.modified_count != 1:
            raise OrderError("موجودی کافی نیست")
        reserved = quantity
    ttl = await st.get_int("order_ttl_minutes")
    oid = new_id()
    order = {
        "public_id": oid,
        "user_id": user_id,
        "product_id": product_id,
        "product_name": product["name"],
        "quantity": quantity,
        "target_username": target,                 # Stars destination account (None for other products)
        "unit_price_toman": unit,
        "total_toman": total_toman,
        "stars_amount": stars_total,
        "amount": stars_total if payment_method == "stars" else total_toman,       # what the customer pays...
        "currency": "XTR" if payment_method == "stars" else "IRT",               # ...and in which unit
        "price_snapshot": {k: v for k, v in quote.items() if k != "_id"},
        "delivery_spec": delivery_spec(product),   # frozen: product edits never change a paid order
        "stock_reserved": reserved,
        "payment_method": payment_method,
        "status": S.PENDING_PAYMENT,
        "payment_status": "UNPAID",
        "fulfillment_status": "NONE",
        "open_key": open_key,
        "paid_charge_id": None,
        "created_at": now,
        "updated_at": now,
        "expires_at": now + timedelta(minutes=ttl),
        "paid_at": None,
        "completed_at": None,
        "history": [{"to": S.PENDING_PAYMENT, "at": now, "note": "created"}],
    }
    try:
        await db.orders.insert_one(order)
    except DuplicateKeyError:
        if reserved:   # somebody else created the same open order first: give the reservation back
            await db.products.update_one({"public_id": product_id, "stock": {"$ne": None}}, {"$inc": {"stock": reserved}})
        return await db.orders.find_one({"open_key": open_key})
    return order


async def _release_stock(order: dict) -> None:
    """Give reserved stock back exactly once (guarded by the stock_reserved value on the order)."""
    n = int(order.get("stock_reserved") or 0)
    if n <= 0:
        return
    db = get_db()
    res = await db.orders.update_one({"public_id": order["public_id"], "stock_reserved": n}, {"$set": {"stock_reserved": 0}})
    if res.modified_count == 1:
        await db.products.update_one({"public_id": order["product_id"], "stock": {"$ne": None}}, {"$inc": {"stock": n}})


async def get_order(public_id: str) -> dict | None:
    return await get_db().orders.find_one({"public_id": public_id})


async def list_user_orders(user_id: int, limit: int = 10) -> list[dict]:
    return [o async for o in get_db().orders.find({"user_id": user_id}).sort("created_at", -1).limit(limit)]


async def transition(public_id: str, to: str, *, from_states: list[str] | None = None,
                     set_fields: dict | None = None, note: str = "") -> dict | None:
    """Atomic compare-and-set transition. Returns the updated order, or None if the order was not in an
    allowed source state (=> somebody else already moved it; callers treat None as 'do nothing')."""
    if to not in ALL_STATUSES:
        raise OrderError("unknown status")
    allowed = sources_for(to)
    if from_states is not None:
        for s in from_states:
            if not can_transition(s, to):
                raise OrderError(f"illegal transition {s} -> {to}")
        allowed = [s for s in allowed if s in from_states]
    now = utcnow()
    fields = {"status": to, "updated_at": now}
    if to != S.PENDING_PAYMENT:
        fields["open_key"] = f"closed:{public_id}"
    if set_fields:
        fields.update(set_fields)
    updated = await get_db().orders.find_one_and_update(
        {"public_id": public_id, "status": {"$in": allowed}},
        {"$set": fields, "$push": {"history": {"to": to, "at": now, "note": note}}},
        return_document=ReturnDocument.AFTER,
    )
    if updated:
        never_paid = not updated.get("paid_at")
        undelivered_refund = to == S.REFUNDED and updated.get("fulfillment_status") != "DONE"
        if (to in (S.CANCELLED, S.FAILED) and never_paid) or undelivered_refund:
            await _release_stock(updated)
    return updated


async def expire_pending_orders() -> int:
    db = get_db()
    n = 0
    async for o in db.orders.find({"status": S.PENDING_PAYMENT, "expires_at": {"$lt": utcnow()}}):
        if await transition(o["public_id"], S.CANCELLED, from_states=[S.PENDING_PAYMENT], note="expired"):
            n += 1
    return n
