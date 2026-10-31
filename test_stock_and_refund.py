"""Stock reservation (no overselling, no leaks) and refunds. Fake bot / mock DB only."""
from datetime import timedelta

import pytest

from constants import OrderStatus as S
from db import get_db
from payment_manager import get_provider
import fulfillment, orders
from util import utcnow

stars = get_provider("stars")


async def _stock(pid):
    return (await get_db().products.find_one({"public_id": pid}))["stock"]


async def _set_stock(pid, n):
    await get_db().products.update_one({"public_id": pid}, {"$set": {"stock": n}})


async def test_unlimited_stock_is_never_touched(tg):
    await orders.create_order(1, tg.stars, 3)
    assert await _stock(tg.stars) is None


async def test_order_reserves_stock_and_sold_out_blocks_new_orders(tg):
    await _set_stock(tg.stars, 2)
    o = await orders.create_order(1, tg.stars, 2)
    assert o["stock_reserved"] == 2 and await _stock(tg.stars) == 0
    with pytest.raises(orders.OrderError):
        await orders.create_order(2, tg.stars, 1)


async def test_quantity_above_stock_is_refused(tg):
    await _set_stock(tg.stars, 1)
    with pytest.raises(orders.OrderError, match="موجودی"):
        await orders.create_order(1, tg.stars, 2)
    assert await _stock(tg.stars) == 1                 # nothing was taken


async def test_reusing_the_open_order_does_not_reserve_twice(tg):
    await _set_stock(tg.stars, 5)
    a = await orders.create_order(1, tg.stars, 2)
    b = await orders.create_order(1, tg.stars, 2)
    assert a["public_id"] == b["public_id"] and await _stock(tg.stars) == 3


async def test_expired_order_gives_stock_back_once(tg):
    await _set_stock(tg.stars, 3)
    o = await orders.create_order(1, tg.stars, 2)
    await get_db().orders.update_one({"public_id": o["public_id"]}, {"$set": {"expires_at": utcnow() - timedelta(minutes=1)}})
    assert await orders.expire_pending_orders() == 1
    assert await _stock(tg.stars) == 3
    assert await orders.expire_pending_orders() == 0
    assert await _stock(tg.stars) == 3                 # no double release


async def test_payment_mismatch_releases_stock(tg, sent):
    await _set_stock(tg.stars, 3)
    o = await orders.create_order(1, tg.stars, 1)
    res = await stars.handle_successful_payment(1, o["public_id"], "XTR", 1, "chg-bad")
    assert res.code == "MISMATCH" and await _stock(tg.stars) == 3


async def test_paid_order_keeps_its_stock(tg, sent):
    await _set_stock(tg.stars, 3)
    o = await orders.create_order(1, tg.stars, 1)
    res = await stars.handle_successful_payment(1, o["public_id"], "XTR", o["stars_amount"], "chg-1")
    await fulfillment.on_order_paid(res.order)
    assert await _stock(tg.stars) == 2


async def test_release_never_creates_stock_for_unlimited_products(tg):
    o = await orders.create_order(1, tg.stars, 1)
    await orders._release_stock({**o, "stock_reserved": 1})
    assert await _stock(tg.stars) is None


# ------------------------------------------------------------------ refunds
async def _paid_stars_order(tg, stock=None, qty=1):
    if stock is not None:
        await _set_stock(tg.stars, stock)
    o = await orders.create_order(7, tg.stars, qty)
    res = await stars.handle_successful_payment(7, o["public_id"], "XTR", o["stars_amount"], "chg-r1")
    await fulfillment.on_order_paid(res.order)
    return await orders.get_order(o["public_id"])


async def test_refund_of_undelivered_manual_order(tg, sent):
    order = await _paid_stars_order(tg, stock=4, qty=2)
    assert order["status"] == S.PROCESSING and await _stock(tg.stars) == 2
    assert await stars.refund(order, bot=tg.bot, actor="admin1")
    assert tg.bot.refunds == [(7, "chg-r1")]
    cur = await orders.get_order(order["public_id"])
    assert cur["status"] == S.REFUNDED and cur["payment_status"] == "REFUNDED"
    assert (await get_db().payments.find_one({"_id": "stars:chg-r1"}))["status"] == "REFUNDED"
    assert await _stock(tg.stars) == 4                 # never delivered => stock is back


async def test_refund_runs_only_once(tg, sent):
    order = await _paid_stars_order(tg)
    assert await stars.refund(order, bot=tg.bot)
    assert not await stars.refund(order, bot=tg.bot)
    assert len(tg.bot.refunds) == 1


async def test_refund_of_delivered_order_does_not_restock(tg, sent):
    order = await _paid_stars_order(tg, stock=4)
    await fulfillment.complete_manual(order["public_id"], "admin1", "done")
    order = await orders.get_order(order["public_id"])
    assert order["status"] == S.FULFILLED
    assert await stars.refund(order, bot=tg.bot)
    assert await _stock(tg.stars) == 3                 # the item was delivered, so it stays sold


async def test_failed_refund_returns_order_to_failed_and_can_be_retried(tg, sent):
    order = await _paid_stars_order(tg)

    async def boom(**kw):
        raise RuntimeError("telegram down")

    original = tg.bot.refund_star_payment
    tg.bot.refund_star_payment = boom
    assert not await stars.refund(order, bot=tg.bot)
    assert (await orders.get_order(order["public_id"]))["status"] == S.FAILED
    tg.bot.refund_star_payment = original
    assert await stars.refund(await orders.get_order(order["public_id"]), bot=tg.bot)
    assert (await orders.get_order(order["public_id"]))["status"] == S.REFUNDED
