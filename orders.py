"""Orders: price snapshot at creation, strict state machine, atomic transitions."""
from __future__ import annotations

from datetime import timedelta

from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError

from constants import ALL_STATUSES, OrderStatus as S, can_transition, sources_for
from db import get_db
import settings as st
from catalog import get_product, purchasable
from util import as_aware, new_id, utcnow


class OrderError(Exception):
    pass


async def create_order(user_id: int, product_id: str, quantity: int, payment_method: str = "stars") -> dict:
    """Create (or reuse the still-open) order. The price is FROZEN here and never recomputed."""
    if quantity < 1 or quantity > 20:
        raise OrderError("invalid quantity")
    product = await get_product(product_id)
    if not product:
        raise OrderError("product not found")
    ok, reason, quote = await purchasable(product)
    if not ok:
        raise OrderError(reason)
    db = get_db()
    open_key = f"{user_id}:{product_id}:{quantity}:{payment_method}"
    now = utcnow()

    existing = await db.orders.find_one({"open_key": open_key})
    if existing:
        if as_aware(existing["expires_at"]) > now:
            return existing            # duplicate order prevention: reuse the open order (price stays locked)
        await transition(existing["public_id"], S.CANCELLED, note="expired")

    unit = int(quote["final_toman"])
    total_toman = unit * quantity
    stars_total = None
    if quote.get("toman_per_star"):
        from money import stars_for_toman
        stars_total = stars_for_toman(total_toman, quote["toman_per_star"])
    ttl = await st.get_int("order_ttl_minutes")
    oid = new_id()
    order = {
        "public_id": oid,
        "user_id": user_id,
        "product_id": product_id,
        "product_name": product["name"],
        "quantity": quantity,
        "unit_price_toman": unit,
        "total_toman": total_toman,
        "stars_amount": stars_total,
        "price_snapshot": {k: v for k, v in quote.items() if k != "_id"},
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
        return await db.orders.find_one({"open_key": open_key})
    return order


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
    return await get_db().orders.find_one_and_update(
        {"public_id": public_id, "status": {"$in": allowed}},
        {"$set": fields, "$push": {"history": {"to": to, "at": now, "note": note}}},
        return_document=ReturnDocument.AFTER,
    )


async def expire_pending_orders() -> int:
    db = get_db()
    n = 0
    async for o in db.orders.find({"status": S.PENDING_PAYMENT, "expires_at": {"$lt": utcnow()}}):
        if await transition(o["public_id"], S.CANCELLED, from_states=[S.PENDING_PAYMENT], note="expired"):
            n += 1
    return n
