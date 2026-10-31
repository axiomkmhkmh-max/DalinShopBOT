"""Background loops (single process, Mongo lock so extra replicas do not double-run)."""
from __future__ import annotations

import asyncio
import logging
from datetime import timedelta

from pymongo.errors import DuplicateKeyError

from config import get_config
from constants import OrderStatus as S
from db import get_db
import fulfillment, notifier, orders, telegram_supply
import settings as st
import pricing_engine as engine
from pricing_providers import build_rate_provider
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
    """Scheduled refresh: (1) rate API  (2) Telegram gift/premium prices, availability, bot balance
    (3) recompute every quote. Respects the on/off switch and the interval set in the dashboard."""
    db = get_db()
    if not force and not await st.get_bool("auto_refresh_enabled"):
        return None
    interval = timedelta(minutes=await st.get_int("refresh_interval_minutes"))
    state = await db.settings.find_one({"key": "_pricing_last_run"})
    last = as_aware(state["value"]) if state else None
    if not force and last and utcnow() - last < interval:
        return None
    if not await acquire_lock("pricing", 120):
        return None
    try:
        supply = await telegram_supply.sync_supply(notifier.get_bot())
        report = await engine.refresh_all(build_rate_provider(get_config()), fetch=True)
        report["supply"] = supply
        await db.settings.update_one({"key": "_pricing_last_run"}, {"$set": {"value": utcnow()}}, upsert=True)
        return report
    finally:
        await db.locks.delete_one({"_id": "pricing"})      # the lock only protects a run in progress


async def run_order_maintenance() -> None:
    await orders.expire_pending_orders()
    db = get_db()
    cutoff = utcnow() - timedelta(minutes=2)
    async for o in db.orders.find({"status": S.PAID, "paid_at": {"$lt": cutoff}}):
        await fulfillment.fulfill_order(o["public_id"])      # recovery: paid but never claimed
    # Claimed for delivery but no outcome was ever recorded (crash / restart): flag once for the admin.
    stuck = utcnow() - timedelta(minutes=10)
    async for o in db.orders.find({"status": S.PROCESSING, "fulfillment_status": "PROCESSING", "updated_at": {"$lt": stuck}}):
        res = await db.orders.update_one({"public_id": o["public_id"], "fulfillment_status": "PROCESSING"},
                                         {"$set": {"fulfillment_status": "MANUAL_PENDING"}})
        if res.modified_count == 1:
            await notifier.send_admins(
                f"سفارش {o['public_id']} در حال پردازش گیر کرده است (نتیجه تحویل ثبت نشد). "
                "قبل از هر اقدام در تلگرام بررسی کنید گیرنده محصول را گرفته یا نه، سپس «تحویل شد» یا بازپرداخت بزنید.")


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
