from datetime import timedelta
from decimal import Decimal

import pytest

from config import Config
from db import get_db
from money import MoneyError
import catalog, orders
import settings as st
import pricing_engine as engine, pricing_providers as providers
from pricing_providers import MockRateProvider, ProviderError
from util import utcnow


async def test_quote_computed_and_persisted(shop):
    q = await engine.get_valid_quote(shop.pid)
    assert q["final_toman"] == 205000
    assert q["rate_toman_per_unit"] == "100000"
    assert q["toman_per_star"] == "2000.00"
    assert q["stars_amount"] == 103
    assert q["updated_at"] is not None and q["rate_fetched_at"] is not None


async def test_profit_setting_above_limit_rejected(db):
    with pytest.raises(MoneyError):
        await st.set_setting("profit_toman", "7001")
    assert await st.set_setting("profit_toman", "7000") == "7000"


async def test_rounding_is_configurable(shop):
    await st.set_setting("rounding_step", "5000")
    await st.set_setting("rounding_mode", "nearest")
    await engine.refresh_all(shop.rates, fetch=False)
    q = await engine.get_valid_quote(shop.pid)
    assert q["final_toman"] % 5000 == 0


async def test_rial_rate_provider_converts_to_toman(monkeypatch):
    async def fake_get(*a, **k):
        return {"data": {"rate": Decimal("1250000")}}          # TEST value in Rial

    monkeypatch.setattr(providers, "_http_get_json", fake_get)
    cfg = Config(rate_api_url="https://example.invalid/{currency}", rate_json_path="data.rate", rate_unit="IRR")
    rate = await providers.HttpJsonRateProvider(cfg).get_rate("USD")
    assert rate.toman_per_unit == Decimal("125000")


async def test_unconfigured_http_provider_fails_cleanly():
    with pytest.raises(ProviderError):
        await providers.HttpJsonRateProvider(Config(rate_api_url="", rate_json_path="")).get_rate("USD")


async def test_api_failure_does_not_overwrite_quote(shop):
    before = await engine.get_valid_quote(shop.pid)
    report = await engine.refresh_all(MockRateProvider(fail=True), fetch=True)
    assert report["rates"]["USD"].startswith("error")
    after = await engine.get_valid_quote(shop.pid)      # still within max age, but NOT updated
    assert after["final_toman"] == before["final_toman"]
    assert after["rate_fetched_at"] == before["rate_fetched_at"]


async def test_stale_rate_blocks_purchase(shop):
    old = utcnow() - timedelta(hours=5)                 # max_age default is 120 minutes
    await get_db().price_quotes.update_one({"product_id": shop.pid}, {"$set": {"rate_fetched_at": old}})
    with pytest.raises(engine.PriceUnavailable):
        await engine.get_valid_quote(shop.pid)
    product = await catalog.get_product(shop.pid)
    ok, reason, _ = await catalog.purchasable(product)
    assert not ok
    with pytest.raises(orders.OrderError):
        await orders.create_order(1, shop.pid, 1)


async def test_missing_rate_means_not_purchasable(db, sent):
    await st.set_setting("stars_usd_value", "0.02")
    pid = await catalog.save_product({"name": "x", "enabled": True, "kind": "other", "supplier_key": "manual",
                                      "pricing": {"source": "manual", "manual_amount": "2", "manual_currency": "USD"}})
    await engine.refresh_all(MockRateProvider(fail=True), fetch=True)
    with pytest.raises(engine.PriceUnavailable):
        await engine.get_valid_quote(pid)


async def test_old_order_price_is_frozen_after_rate_update(shop):
    o1 = await orders.create_order(7, shop.pid, 1)
    assert o1["unit_price_toman"] == 205000
    shop.rates.rates["USD"] = "120000"                  # new TEST rate
    await engine.refresh_all(shop.rates, fetch=True)
    fresh = await engine.get_valid_quote(shop.pid)
    assert fresh["final_toman"] == 245000
    again = await orders.get_order(o1["public_id"])
    assert again["unit_price_toman"] == 205000 and again["price_snapshot"]["final_toman"] == 205000
    o2 = await orders.create_order(7, shop.pid, 2)      # different qty => new order at the new price
    assert o2["unit_price_toman"] == 245000


# ---------------------------------------------------------------- manual rate, Stars base, Telegram base
async def test_manual_rate_is_used_and_marked_manual(db):
    await engine.set_manual_rate("USD", "100000")
    doc = await get_db().exchange_rates.find_one({"_id": "USD"})
    assert doc["source"] == "manual" and doc["toman_per_unit"] == "100000"
    with pytest.raises(MoneyError):
        await engine.set_manual_rate("USD", "0")


async def test_manual_rate_has_its_own_longer_expiry(shop):
    # automatic inputs live 120 min, a rate typed by hand lives 1440 min (both are admin settings)
    three_hours = utcnow() - timedelta(hours=3)
    await get_db().price_quotes.update_one(
        {"product_id": shop.pid}, {"$set": {"rate_fetched_at": three_hours, "usd_rate_fetched_at": three_hours,
                                            "rate_source": "manual", "usd_rate_source": "manual"}})
    assert (await engine.get_valid_quote(shop.pid))["final_toman"] == 205000       # manual: still fresh
    await get_db().price_quotes.update_one({"product_id": shop.pid}, {"$set": {"rate_source": "http_json"}})
    with pytest.raises(engine.PriceUnavailable):                                    # automatic: stale
        await engine.get_valid_quote(shop.pid)
    await get_db().price_quotes.update_one(
        {"product_id": shop.pid}, {"$set": {"rate_source": "manual", "rate_fetched_at": utcnow() - timedelta(hours=25)}})
    with pytest.raises(engine.PriceUnavailable):                                    # manual too old (> 24h)
        await engine.get_valid_quote(shop.pid)


async def test_stale_usd_rate_blocks_even_toman_priced_product(db, sent):
    await st.set_setting("stars_usd_value", "0.02")
    await engine.set_manual_rate("USD", "100000")
    pid = await catalog.save_product({"name": "t", "enabled": True, "kind": "other", "supplier_key": "manual",
                                      "pricing": {"source": "manual", "manual_amount": "200000", "manual_currency": "IRT"}})
    await engine.refresh_all(None, fetch=False)
    assert (await engine.get_valid_quote(pid))["stars_amount"]
    await get_db().price_quotes.update_one(
        {"product_id": pid}, {"$set": {"usd_rate_fetched_at": utcnow() - timedelta(hours=30)}})
    with pytest.raises(engine.PriceUnavailable):        # the Stars price depends on the USD rate
        await engine.get_valid_quote(pid)


async def test_unconfigured_rate_api_does_not_overwrite_manual_rate(db):
    await engine.set_manual_rate("USD", "100000")
    rep = await engine.refresh_rates(providers.HttpJsonRateProvider(Config(rate_api_url="", rate_json_path="")), {"USD"})
    assert rep["USD"].startswith("not configured")
    doc = await get_db().exchange_rates.find_one({"_id": "USD"})
    assert doc["source"] == "manual" and doc["last_error"] is None


async def test_stars_base_price_uses_cost_per_star(tg):
    q = await engine.get_valid_quote(tg.stars)
    assert q["base_currency"] == "XTR" and q["base_amount"] == "100"
    assert q["cost_toman"] == "150000.000"             # 100 Stars x (0.015 USD x 100000)
    assert q["final_toman"] == 155000                  # + 5000 profit
    assert q["stars_amount"] == 78                     # ceil(155000/2000) = 78 -> 156000 <= 157000 cap


async def test_xtr_product_needs_stars_cost_setting(db, sent):
    await engine.set_manual_rate("USD", "100000")
    await st.set_setting("stars_usd_value", "0.02")
    pid = await catalog.save_product({"name": "s", "enabled": True, "kind": "stars", "supplier_key": "manual",
                                      "pricing": {"source": "manual", "manual_amount": "100", "manual_currency": "XTR"}})
    await engine.refresh_all(None, fetch=False)
    with pytest.raises(engine.PriceUnavailable):
        await engine.get_valid_quote(pid)


async def test_telegram_priced_products(tg):
    gift = await engine.get_valid_quote(tg.gift)
    assert gift["cost_toman"] == "75000.000" and gift["final_toman"] == 80000 and gift["stars_amount"] == 40
    prem = await engine.get_valid_quote(tg.prem)
    assert prem["cost_toman"] == "1500000.000" and prem["final_toman"] == 1505000 and prem["stars_amount"] == 753
    assert int(prem["stars_profit_toman"]) <= 7000


async def test_real_profit_in_stars_never_above_7000(tg):
    await st.set_setting("profit_toman", "7000")
    await engine.refresh_all(None, fetch=False)
    q = await engine.get_valid_quote(tg.prem)
    assert q["final_toman"] == 1507000
    assert q["stars_amount"] == 753 and int(q["stars_profit_toman"]) == 6000     # plain ceil would be 754 => 8000


async def test_auto_refresh_switch(tg, fake_bot):
    import background
    await st.set_setting("auto_refresh_enabled", "false")
    assert await background.run_pricing_cycle() is None            # scheduled run is skipped
    fake_bot.balance = 777
    assert await background.run_pricing_cycle(force=True) is not None   # manual "refresh now" still works
    assert (await tg.ts.get_supply_status())["bot_balance"] == 777
    await st.set_setting("auto_refresh_enabled", "true")
    await get_db().settings.delete_one({"key": "_pricing_last_run"})
    assert await background.run_pricing_cycle() is not None
