from __future__ import annotations

from datetime import timedelta

from config import get_config
from db import get_db
from pricing_engine import PriceUnavailable, get_valid_quote
import settings as st
from util import as_aware, new_id, utcnow

AUTO_DELIVERIES = ("auto_gift", "auto_premium")


def needs_supply(product: dict) -> bool:
    """Gift / Premium products (priced from, or delivered through, Telegram)."""
    return (product.get("delivery") in AUTO_DELIVERIES
            or (product.get("pricing") or {}).get("source") == "telegram")


async def list_categories() -> list[dict]:
    return [c async for c in get_db().categories.find({"enabled": True}).sort("sort", 1)]


async def list_products(category_id: str | None = None, kind: str | None = None) -> list[dict]:
    q: dict = {"enabled": True}
    if category_id:
        q["category_id"] = category_id
    if kind:
        q["kind"] = kind
    return [p async for p in get_db().products.find(q).sort("sort", 1)]


async def get_product(public_id: str) -> dict | None:
    return await get_db().products.find_one({"public_id": public_id})


DELIVERY_FA = {"stars_auto": "Stars: خودکار از ۵۰ به بالا اگر روش رسمی باشد، وگرنه دستی", "auto_gift": "خودکار (گیفت تلگرام)", "auto_premium": "خودکار (Premium تلگرام)", "manual": "دستی"}


def delivery_spec(product: dict) -> dict:
    """What must be delivered. Copied into the order when it is created, so editing the product later can
    never change an order that is already paid."""
    return {k: product.get(k) for k in ("delivery", "gift_id", "amount", "supplier_key", "kind")}


async def purchasable(product: dict) -> tuple[bool, str, dict | None]:
    """(ok, reason, quote). A product is never shown as available without a fresh price, real stock and a valid supplier."""
    if not product.get("enabled"):
        return False, "غیرفعال", None
    supplier_key = product.get("supplier_key") or "manual"
    sup = await get_db().suppliers.find_one({"key": supplier_key})
    if supplier_key in ("manual", "telegram", "stars") and sup is None:
        pass  # built-in suppliers always exist
    elif sup is None or not sup.get("enabled"):
        return False, "تأمین‌کننده فعال نیست", None
    elif sup.get("type") == "mock" and get_config().is_production:
        return False, "تأمین‌کننده نامعتبر", None

    # Stock typed by the admin (empty = unlimited). Stock is RESERVED when an order is created (orders.py).
    stock = product.get("stock")
    if stock is not None and stock < 1:
        return False, "ناموجود", None

    # Products priced/delivered through Telegram need fresh data from Telegram.
    if needs_supply(product):
        supply = product.get("supply") or {}
        checked = as_aware(supply.get("checked_at"))
        max_age = timedelta(minutes=await st.get_int("max_age_minutes"))
        if checked is None or utcnow() - checked > max_age:
            return False, "اطلاعات تلگرام قدیمی است؛ موقتاً قابل خرید نیست", None
        if not supply.get("available"):
            return False, "ناموجود", None

    try:
        quote = await get_valid_quote(product["public_id"])
    except PriceUnavailable:
        return False, "قیمت در حال به‌روزرسانی است؛ موقتاً قابل خرید نیست", None
    if not quote.get("stars_amount"):
        return False, "نرخ Stars تنظیم نشده است", None
    return True, "", quote


async def save_product(data: dict, public_id: str | None = None) -> str:
    db = get_db()
    now = utcnow()
    if public_id:
        await db.products.update_one({"public_id": public_id}, {"$set": {**data, "updated_at": now}})
        return public_id
    pid = new_id()
    await db.products.insert_one({"public_id": pid, "created_at": now, "updated_at": now, "sort": 100, **data})
    return pid


async def save_category(name: str, public_id: str | None = None, enabled: bool = True) -> str:
    db = get_db()
    if public_id:
        await db.categories.update_one({"public_id": public_id}, {"$set": {"name": name, "enabled": enabled}})
        return public_id
    pid = new_id()
    await db.categories.insert_one({"public_id": pid, "name": name, "enabled": enabled, "sort": 100})
    return pid
