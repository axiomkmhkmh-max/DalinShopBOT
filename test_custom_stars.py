"""Custom Stars amount (typed by the customer). TEST numbers from the `tg` fixture: 1 Star costs 1500 Toman, is worth 2000
Toman to the customer, profit 5000, rounding step 1000 -> 100 Stars = 155000 Toman = 78 Stars (profit inside the 7000 cap)."""
from datetime import timedelta
from decimal import Decimal

import pytest

import catalog, fulfillment, orders, wallet
import settings as st
from db import get_db
from payment_wallet import WalletProvider
from util import utcnow


@pytest.fixture
async def custom(tg):
    await st.set_setting("custom_stars_enabled", "true")
    await st.set_setting("wallet_payment_enabled", "true")
    product = await catalog.ensure_custom_stars_product()
    return product["public_id"]


async def test_disabled_by_default(tg):
    pid = (await catalog.ensure_custom_stars_product())["public_id"]
    with pytest.raises(orders.OrderError):
        await orders.create_order(5, pid, 100, "wallet")


async def test_price_uses_same_formula_and_cap_as_packages(tg, custom):
    o = await orders.create_order(5, custom, 100, "wallet", target_username="@Bob_99")
    assert o["quantity"] == 100 and o["total_toman"] == 155_000 and o["stars_amount"] == 78
    snap = o["price_snapshot"]
    assert Decimal(snap["effective_profit_toman"]) == 5000 and Decimal(snap["stars_profit_toman"]) <= 7000


@pytest.mark.parametrize("n", [50, 77, 100, 333, 1000, 4321, 10000])
async def test_profit_never_exceeds_cap_for_any_amount(tg, custom, n):
    o = await orders.create_order(5, custom, n, "wallet")
    snap = o["price_snapshot"]
    cost = Decimal(snap["cost_toman"])
    assert cost <= o["total_toman"] <= cost + 7000
    assert Decimal(snap["stars_profit_toman"]) <= 7000


@pytest.mark.parametrize("n", [0, -1, 49, 10_001, 10**9])
async def test_limits_are_enforced(tg, custom, n):
    with pytest.raises(orders.OrderError):
        await orders.create_order(5, custom, n, "wallet")


async def test_limits_come_from_settings(tg, custom):
    await st.set_setting("custom_stars_min", "10")
    await st.set_setting("custom_stars_max", "20")
    assert (await orders.create_order(5, custom, 10, "wallet"))["quantity"] == 10
    with pytest.raises(orders.OrderError):
        await orders.create_order(5, custom, 21, "wallet")


async def test_stale_or_missing_inputs_stop_sales(tg, custom, db):
    await db.exchange_rates.update_one({"_id": "USD"}, {"$set": {"fetched_at": utcnow() - timedelta(days=3)}})
    with pytest.raises(orders.OrderError):
        await orders.create_order(5, custom, 100, "wallet")
    await db.exchange_rates.update_one({"_id": "USD"}, {"$set": {"fetched_at": utcnow()}})
    await db.settings.delete_one({"key": "stars_cost_usd"})
    with pytest.raises(orders.OrderError):
        await orders.create_order(5, custom, 100, "wallet")


async def test_price_is_frozen_in_the_order(tg, custom):
    import pricing_engine as engine
    o = await orders.create_order(5, custom, 100, "wallet")
    await engine.set_manual_rate("USD", "200000")
    assert (await orders.get_order(o["public_id"]))["total_toman"] == 155_000


async def test_hidden_helper_product_is_not_listed_as_a_package(tg, custom):
    listed = [p["public_id"] for p in await catalog.list_products(kind="stars")]
    assert custom not in listed and tg.stars in listed


async def test_pricing_cycle_ignores_the_helper_product(tg, custom):
    import pricing_engine as engine
    report = await engine.refresh_all(None, fetch=False)
    assert custom not in report["products"] and await get_db().price_quotes.count_documents({"product_id": custom}) == 0


async def test_full_flow_wallet_pays_and_sender_gets_the_typed_amount(tg, custom, stars_sender, sent):
    await wallet.credit(5, 1_000_000, idem_key="seed")
    o = await orders.create_order(5, custom, 100, "wallet", target_username="@Bob_99")
    assert await fulfillment.on_order_paid(await WalletProvider().pay(o)) == "DELIVERED"
    assert stars_sender.sent == [(5, 100, f"order:{o['public_id']}")] and stars_sender.targets == ["Bob_99"]
    assert await wallet.get_balance(5) == 1_000_000 - 155_000
