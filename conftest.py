"""Shared fixtures. All values below are TEST DATA ONLY — not real rates or prices.
DB: mongomock-motor (in-memory). NOTE: mongomock does not reproduce real MongoDB concurrency semantics."""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from mongomock_motor import AsyncMongoMockClient

import db as dbmod
import catalog, notifier
import settings as st
import pricing_engine as engine
from pricing_providers import MockPriceProvider, MockRateProvider
import supplier_base as sup_base


@pytest.fixture
async def db():
    d = AsyncMongoMockClient()["test_market"]
    dbmod.set_db(d)
    await dbmod.ensure_indexes(d)
    sup_base._override.clear()
    return d


@pytest.fixture
def sent(monkeypatch):
    box = SimpleNamespace(users=[], admins=[])

    async def send_user(uid, text, **kw):
        box.users.append((uid, text))
        return True

    async def send_admins(text):
        box.admins.append(text)

    monkeypatch.setattr(notifier, "send_user", send_user)
    monkeypatch.setattr(notifier, "send_admins", send_admins)
    return box


@pytest.fixture
async def shop(db, sent):
    """One product: base 2 USD (TEST value), USD rate 100000 Toman (TEST value), profit 5000, Star = 0.02 USD (TEST)."""
    await st.set_setting("profit_toman", "5000")
    await st.set_setting("rounding_step", "1000")
    await st.set_setting("stars_usd_value", "0.02")
    pid = await catalog.save_product({
        "name": "Premium 3 ماهه", "description": "test", "kind": "premium", "enabled": True,
        "pricing": {"source": "manual", "manual_amount": "2", "manual_currency": "USD"},
        "supplier_key": "manual", "digital_in_telegram": True,
    })
    rates = MockRateProvider({"USD": "100000"})
    await engine.refresh_all(MockPriceProvider(), rates, fetch=True)
    return SimpleNamespace(pid=pid, rates=rates)
