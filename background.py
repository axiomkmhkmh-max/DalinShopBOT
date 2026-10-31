"""Background loops (single process, Mongo lock so extra replicas do not double-run)."""
from __future__ import annotations

import asyncio
import logging
from datetime import timedelta

from pymongo.errors import DuplicateKeyError

from config import get_config
from constants import OrderStatus as S
from db import get_db
import fulfillment, orders
import settings as st
import pricing_engine as engine
from pricing_providers import ProviderError, build_price_provider, build_rate_provider
from util import as_aware, utcnow

log = logging.getLogger(__name__)


async def acquire_lock(name: str, ttl_seconds: int) -> bool:
    db = get_db()
    now = utcnow()
    try:
        await db.locks.insert_one({"_id": name, "expires_at": now + timedelta(seconds=ttl_seconds)})
        return True
    except DuplicateKeyError:
        res = await db.locks.update_one(
            {"_id": name, "expires_at": {"$lt": now}},
            {"$set": {"expires_at": now + timedelta(seconds=ttl_seconds)}},
        )
        return res.modified_count == 1


async def run_pricing_cycle(force: bool = False) -> dict | None:
    """Respects the admin-configured interval so the upstream API is not hit more than needed."""
    db = get_db()
    interval = timedelta(minutes=await st.get_int("refresh_interval_minutes"))
    state = await db.settings.find_one({"key": "_pricing_last_run"})
    last = as_aware(state["value"]) if state else None
    if not force and last and utcnow() - last < interval:
        return None
    if not await acquire_lock("pricing", 120):
        return None
    cfg = get_config()
    try:
        pp = build_price_provider(await st.get_setting("price_provider"), cfg)
        rp = build_rate_provider(await st.get_setting("rate_provider"), cfg)
    except ProviderError:
        log.exception("pricing providers misconfigured")
        return None
    report = await engine.refresh_all(pp, rp, fetch=True)
    await db.settings.update_one({"key": "_pricing_last_run"}, {"$set": {"value": utcnow()}}, upsert=True)
    return report


async def run_order_maintenance() -> None:
    await orders.expire_pending_orders()
    cutoff = utcnow() - timedelta(minutes=2)
    async for o in get_db().orders.find({"status": S.PAID, "paid_at": {"$lt": cutoff}}):
        await fulfillment.fulfill_order(o["public_id"])      # recovery: paid but never claimed


async def loop(name: str, coro_factory, every: int) -> None:
    while True:
        try:
            await coro_factory()
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("background task %s failed", name)
        await asyncio.sleep(every)


def start_all() -> list[asyncio.Task]:
    return [
        asyncio.create_task(loop("pricing", run_pricing_cycle, 60)),
        asyncio.create_task(loop("orders", run_order_maintenance, 60)),
    ]
