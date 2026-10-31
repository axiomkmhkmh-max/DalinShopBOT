from datetime import timedelta
from decimal import Decimal

import pytest

from config import Config
from db import get_db
from money import MoneyError
import catalog, orders
import settings as st
import pricing_engine as engine, pricing_providers as providers
from pricing_providers import MockPriceProvider, MockRateProvider, ProviderError
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
    await engine.refresh_all(MockPriceProvider(), shop.rates, fetch=False)
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
    report = await engine.refresh_all(MockPriceProvider(), MockRateProvider(fail=True), fetch=True)
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
    await engine.refresh_all(MockPriceProvider(), MockRateProvider(fail=True), fetch=True)
    with pytest.raises(engine.PriceUnavailable):
        await engine.get_valid_quote(pid)


async def test_old_order_price_is_frozen_after_rate_update(shop):
    o1 = await orders.create_order(7, shop.pid, 1)
    assert o1["unit_price_toman"] == 205000
    shop.rates.rates["USD"] = "120000"                  # new TEST rate
    await engine.refresh_all(MockPriceProvider(), shop.rates, fetch=True)
    fresh = await engine.get_valid_quote(shop.pid)
    assert fresh["final_toman"] == 245000
    again = await orders.get_order(o1["public_id"])
    assert again["unit_price_toman"] == 205000 and again["price_snapshot"]["final_toman"] == 205000
    o2 = await orders.create_order(7, shop.pid, 2)      # different qty => new order at the new price
    assert o2["unit_price_toman"] == 245000


async def test_provider_sourced_product(db, sent):
    await st.set_setting("stars_usd_value", "0.02")
    pid = await catalog.save_product({"name": "p", "enabled": True, "kind": "stars", "supplier_key": "manual",
                                      "pricing": {"source": "provider", "provider_ref": "sku1"}})
    pp = MockPriceProvider({"sku1": ("3", "USD")})
    await engine.refresh_all(pp, MockRateProvider({"USD": "100000"}), fetch=True)
    q = await engine.get_valid_quote(pid)
    assert q["cost_toman"] == "300000"
