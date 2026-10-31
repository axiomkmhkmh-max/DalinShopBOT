"""MongoDB access (Motor). Use set_db() in tests to inject a mongomock database."""
from __future__ import annotations

from motor.motor_asyncio import AsyncIOMotorClient

_client = None
_db = None


async def init_db(uri: str, name: str):
    global _client, _db
    _client = AsyncIOMotorClient(uri, serverSelectionTimeoutMS=8000, tz_aware=True)
    _db = _client[name]
    await _client.admin.command("ping")
    await ensure_indexes(_db)
    return _db


def set_db(db) -> None:
    global _db
    _db = db


def get_db():
    if _db is None:
        raise RuntimeError("database not initialised")
    return _db


async def close_db() -> None:
    if _client is not None:
        _client.close()


async def ensure_indexes(db) -> None:
    await db.users.create_index("telegram_id", unique=True)
    await db.users.create_index("ref_code", unique=True)
    await db.categories.create_index("public_id", unique=True)
    await db.products.create_index("public_id", unique=True)
    await db.orders.create_index("public_id", unique=True)
    await db.orders.create_index("open_key", unique=True)          # one open order per (user, product, qty)
    await db.orders.create_index([("user_id", 1), ("created_at", -1)])
    await db.orders.create_index([("status", 1), ("expires_at", 1)])
    await db.payments.create_index([("order_id", 1)])
    await db.wallets.create_index("user_id", unique=True)
    await db.wallet_ledger.create_index("idem_key", unique=True)    # duplicate-transaction protection
    await db.wallet_ledger.create_index([("user_id", 1), ("created_at", -1)])
    await db.supplier_orders.create_index("order_id", unique=True)  # duplicate-fulfillment protection
    await db.suppliers.create_index("key", unique=True)
    await db.tickets.create_index([("user_id", 1), ("created_at", -1)])
    await db.admin_users.create_index("username", unique=True)
    await db.audit_logs.create_index([("created_at", -1)])
    await db.settings.create_index("key", unique=True)
    await db.price_quotes.create_index("product_id", unique=True)
    await db.locks.create_index("expires_at", expireAfterSeconds=0)
