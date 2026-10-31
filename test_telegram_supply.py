"""Telegram as supplier: sync, availability, automatic Gift/Premium delivery, failure safety. Fake bot only."""
from datetime import timedelta
from types import SimpleNamespace

from aiogram.exceptions import TelegramBadRequest, TelegramNetworkError
from aiogram.methods import SendGift

from conftest import make_gift
from constants import OrderStatus as S
from db import get_db
from payment_manager import get_provider
import catalog, fulfillment, orders
import settings as st
from supplier_base import SupplierRequest
from util import utcnow

stars = get_provider("stars")


def _bad(msg="BALANCE_TOO_LOW"):
    return TelegramBadRequest(method=SendGift(gift_id="g1"), message=msg)


def _net():
    return TelegramNetworkError(method=SendGift(gift_id="g1"), message="timeout")


async def _paid(pid, qty=1, user=1001, charge="chg-1"):
    o = await orders.create_order(user, pid, qty, "stars")
    res = await stars.handle_successful_payment(user, o["public_id"], "XTR", o["stars_amount"], charge)
    assert res.code == "OK"
    return o, res.order


# ------------------------------------------------------------------ sync
async def test_sync_stores_prices_availability_and_balance(tg):
    status = await tg.ts.get_supply_status()
    assert status["ok"] and status["gifts_count"] == 2 and status["bot_balance"] == 5000
    g = await catalog.get_product(tg.gift)
    assert g["supply"]["stars_cost"] == 50 and g["supply"]["available"] and g["supply"]["remaining"] is None
    p = await catalog.get_product(tg.prem)
    assert p["supply"]["stars_cost"] == 1000          # Premium 3 months = 1000 Stars (Bot API rule)
    ok, _, q = await catalog.purchasable(g)
    assert ok and q["stars_amount"] == 40


async def test_gift_missing_or_sold_out_is_not_purchasable(tg):
    tg.bot.gifts = [make_gift("g1", 50, remaining=0)]
    await tg.ts.sync_supply(tg.bot)
    ok, reason, _ = await catalog.purchasable(await catalog.get_product(tg.gift))
    assert not ok and reason == "ناموجود"
    tg.bot.gifts = []
    await tg.ts.sync_supply(tg.bot)
    ok, _, _ = await catalog.purchasable(await catalog.get_product(tg.gift))
    assert not ok
    with __import__("pytest").raises(orders.OrderError):
        await orders.create_order(1, tg.gift, 1)


async def test_telegram_error_keeps_old_data_which_then_ages_out(tg):
    tg.bot.fail_list = _bad("down")
    status = await tg.ts.sync_supply(tg.bot)
    assert not status["ok"] and "getAvailableGifts" in status["error"]
    ok, _, _ = await catalog.purchasable(await catalog.get_product(tg.gift))
    assert ok                                         # last good data is still inside max age
    old = utcnow() - timedelta(hours=3)
    await get_db().products.update_one({"public_id": tg.gift}, {"$set": {"supply.checked_at": old}})
    ok, reason, _ = await catalog.purchasable(await catalog.get_product(tg.gift))
    assert not ok and "قدیمی" in reason               # stale Telegram data stops sales


async def test_no_bot_means_not_purchasable_after_expiry(db, sent):
    status = await __import__("telegram_supply").sync_supply(None)
    assert not status["ok"]


async def test_import_gifts_creates_disabled_products_once(tg):
    tg.bot.gifts = [make_gift("g1", 50), make_gift("g9", 25, emoji="🧸")]
    assert await tg.ts.import_gifts(tg.bot) == 1       # g1 already exists
    p = await get_db().products.find_one({"gift_id": "g9"})
    assert p["enabled"] is False and p["delivery"] == "auto_gift" and p["pricing"] == {"source": "telegram"}
    assert p["supply"]["stars_cost"] == 25 and "🧸" in p["name"]
    assert await tg.ts.import_gifts(tg.bot) == 0


async def test_create_premium_products_once(db, sent):
    import telegram_supply as ts
    assert await ts.create_premium_products() == 3
    assert await ts.create_premium_products() == 0
    months = sorted([p["amount"] async for p in get_db().products.find({"kind": "premium"})])
    assert months == [3, 6, 12]


# ------------------------------------------------------------------ automatic delivery
async def test_gift_is_delivered_automatically_to_the_buyer(tg, sent):
    o, paid = await _paid(tg.gift)
    assert tg.bot.sent_gifts == []                    # nothing sent before the paid order is processed
    assert await fulfillment.on_order_paid(paid) == "DELIVERED"
    assert tg.bot.sent_gifts == [("g1", 1001)]
    assert (await orders.get_order(o["public_id"]))["status"] == S.FULFILLED
    assert any("تحویل داده شد" in t for _, t in sent.users)


async def test_premium_is_delivered_with_the_exact_star_count(tg):
    o, paid = await _paid(tg.prem)
    assert await fulfillment.on_order_paid(paid) == "DELIVERED"
    assert tg.bot.premium == [(1001, 3, 1000)]


async def test_quantity_two_sends_two_units(tg):
    o, paid = await _paid(tg.gift, qty=2)
    assert o["stars_amount"] == 80                    # 40 per unit x 2
    await fulfillment.on_order_paid(paid)
    assert tg.bot.sent_gifts == [("g1", 1001), ("g1", 1001)]


async def test_telegram_refusal_fails_order_and_retry_does_not_duplicate(tg, sent):
    tg.bot.fail_send, tg.bot.fail_after = _bad(), 1   # first unit works, second is refused
    o, paid = await _paid(tg.gift, qty=2)
    assert await fulfillment.on_order_paid(paid) == "FAILED"
    cur = await orders.get_order(o["public_id"])
    assert cur["status"] == S.FAILED and cur["paid_at"] is not None
    assert len(tg.bot.sent_gifts) == 1
    assert any("نیازی به پرداخت مجدد نیست" in t for _, t in sent.users)
    assert any(o["public_id"] in a for a in sent.admins)
    tg.bot.fail_send = None                            # admin tops up the bot and retries
    assert await fulfillment.fulfill_order(o["public_id"]) == "DELIVERED"
    assert len(tg.bot.sent_gifts) == 2                 # exactly 2 in total, not 3
    assert await fulfillment.fulfill_order(o["public_id"]) == "SKIPPED"
    assert len(tg.bot.sent_gifts) == 2


async def test_unknown_outcome_is_never_resent_automatically(tg, sent):
    tg.bot.fail_send = _net()                          # timeout: Telegram may or may not have sent it
    o, paid = await _paid(tg.gift)
    assert await fulfillment.on_order_paid(paid) == "PENDING"
    cur = await orders.get_order(o["public_id"])
    assert cur["status"] == S.PROCESSING and cur["fulfillment_status"] == "MANUAL_PENDING"
    assert any("UNKNOWN" in a for a in sent.admins)
    tg.bot.fail_send = None                            # even if Telegram works again, no automatic re-send
    order = await orders.get_order(o["public_id"])
    product = {**(await catalog.get_product(tg.gift)), **order["delivery_spec"]}
    res = await tg.ts.TelegramSupplier().place_order(SupplierRequest(o["public_id"], product, 1, 1001))
    assert res.status == "PENDING" and tg.bot.sent_gifts == []
    assert await fulfillment.fulfill_order(o["public_id"]) == "SKIPPED"
    assert await fulfillment.complete_manual(o["public_id"], "admin1", "checked")      # admin confirms by hand
    assert (await orders.get_order(o["public_id"]))["status"] == S.FULFILLED


async def test_auto_delivery_switch_off_waits_for_manual(tg, sent):
    await st.set_setting("auto_delivery_enabled", "false")
    o, paid = await _paid(tg.gift)
    assert await fulfillment.on_order_paid(paid) == "PENDING"
    assert tg.bot.sent_gifts == []
    assert (await orders.get_order(o["public_id"]))["fulfillment_status"] == "MANUAL_PENDING"


async def test_paid_order_delivers_what_was_ordered_even_if_product_is_edited(tg):
    o, paid = await _paid(tg.gift)
    await get_db().products.update_one({"public_id": tg.gift}, {"$set": {"gift_id": "g2"}})
    await fulfillment.on_order_paid(paid)
    assert tg.bot.sent_gifts == [("g1", 1001)]        # frozen delivery_spec


async def test_stars_product_is_manual_and_alert_names_the_recipient(tg, sent):
    await get_db().users.insert_one({"telegram_id": 1001, "username": "buyer_x", "ref_code": "r1", "created_at": utcnow()})
    o, paid = await _paid(tg.stars)
    assert await fulfillment.on_order_paid(paid) == "PENDING"
    assert tg.bot.sent_gifts == [] and tg.bot.premium == []
    alert = [a for a in sent.admins if o["public_id"] in a][0]
    assert "@buyer_x" in alert and "1001" in alert and "100 Stars" in alert and "× 1" in alert
    cur = await orders.get_order(o["public_id"])
    assert cur["status"] == S.PROCESSING and cur["fulfillment_status"] == "MANUAL_PENDING"
    assert await fulfillment.complete_manual(o["public_id"], "admin1", "ارسال شد")
    assert (await orders.get_order(o["public_id"]))["status"] == S.FULFILLED


async def test_stuck_processing_order_is_flagged_once(tg, sent):
    import background
    o = await orders.create_order(5, tg.stars, 1, "stars")
    await get_db().orders.update_one({"public_id": o["public_id"]}, {"$set": {
        "status": S.PROCESSING, "fulfillment_status": "PROCESSING", "paid_at": utcnow(),
        "updated_at": utcnow() - timedelta(minutes=30)}})
    await background.run_order_maintenance()
    assert (await orders.get_order(o["public_id"]))["fulfillment_status"] == "MANUAL_PENDING"
    assert sum(o["public_id"] in a for a in sent.admins) == 1
    await background.run_order_maintenance()
    assert sum(o["public_id"] in a for a in sent.admins) == 1


# ------------------------------------------------------------------ secrets
async def test_bot_token_never_reaches_stored_errors(tg, monkeypatch):
    import config
    token = "123456:SECRET-TOKEN-VALUE"
    monkeypatch.setattr(config, "get_config", lambda: SimpleNamespace(
        bot_token=token, rate_api_key="", session_secret="", webhook_secret="", admin_bootstrap_password=""))
    tg.bot.fail_send = _bad(f"bad url https://api.telegram.org/bot{token}/sendGift")
    o, paid = await _paid(tg.gift)
    await fulfillment.on_order_paid(paid)
    cur = await orders.get_order(o["public_id"])
    stored = repr(cur) + repr(await get_db().supplier_orders.find_one({"order_id": o["public_id"]}))
    assert token not in stored and "***" in stored
