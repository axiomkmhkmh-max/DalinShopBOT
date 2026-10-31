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
from pricing_providers import MockRateProvider
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
    await engine.refresh_all(rates, fetch=True)
    return SimpleNamespace(pid=pid, rates=rates)


# ---------------------------------------------------------------- Telegram fakes (TEST DATA ONLY)
class FakeBot:
    """Stands in for aiogram.Bot: records calls, never talks to Telegram."""

    def __init__(self):
        self.gifts: list = []
        self.balance = 5000
        self.sent_gifts: list = []
        self.premium: list = []
        self.refunds: list = []
        self.fail_send: Exception | None = None      # raised by send_gift / gift_premium_subscription
        self.fail_list: Exception | None = None      # raised by get_available_gifts
        self.fail_after: int | None = None           # let N sends succeed, then raise fail_send

    async def get_available_gifts(self):
        if self.fail_list:
            raise self.fail_list
        return SimpleNamespace(gifts=list(self.gifts))

    async def get_my_star_balance(self):
        return SimpleNamespace(amount=self.balance)

    def _maybe_fail(self):
        done = len(self.sent_gifts) + len(self.premium)
        if self.fail_send and (self.fail_after is None or done >= self.fail_after):
            raise self.fail_send

    async def send_gift(self, gift_id, user_id=None, **kw):
        self._maybe_fail()
        self.sent_gifts.append((gift_id, user_id))
        return True

    async def gift_premium_subscription(self, user_id, month_count, star_count, **kw):
        self._maybe_fail()
        self.premium.append((user_id, month_count, star_count))
        return True

    async def refund_star_payment(self, user_id, telegram_payment_charge_id):
        self.refunds.append((user_id, telegram_payment_charge_id))
        return True


def make_gift(gid: str, stars: int, remaining=None, emoji: str = "🎁"):
    return SimpleNamespace(id=gid, star_count=stars, remaining_count=remaining, sticker=SimpleNamespace(emoji=emoji))


@pytest.fixture
def fake_bot(monkeypatch):
    bot = FakeBot()
    monkeypatch.setattr(notifier, "_bot", bot)
    return bot


@pytest.fixture
async def tg(db, sent, fake_bot):
    """Telegram-supplied shop. TEST numbers: 1 USD = 100000 Toman (manual), customer Star = 0.02 USD (2000 Toman),
    1 Star costs the seller 0.015 USD (1500 Toman), profit 5000. Gift g1 = 50 Stars, premium 3 months = 1000 Stars."""
    import telegram_supply as ts

    await st.set_setting("profit_toman", "5000")
    await st.set_setting("rounding_step", "1000")
    await st.set_setting("stars_usd_value", "0.02")
    await st.set_setting("stars_cost_usd", "0.015")
    await engine.set_manual_rate("USD", "100000")
    fake_bot.gifts = [make_gift("g1", 50), make_gift("g2", 100, remaining=3)]
    gift = await catalog.save_product({
        "name": "Gift 50", "kind": "gift", "enabled": True, "delivery": "auto_gift", "gift_id": "g1", "amount": None,
        "supplier_key": "telegram", "stock": None, "pricing": {"source": "telegram"}, "digital_in_telegram": True})
    prem = await catalog.save_product({
        "name": "Premium 3", "kind": "premium", "enabled": True, "delivery": "auto_premium", "amount": 3,
        "gift_id": None, "supplier_key": "telegram", "stock": None, "pricing": {"source": "telegram"},
        "digital_in_telegram": True})
    stars = await catalog.save_product({
        "name": "100 Stars", "kind": "stars", "enabled": True, "delivery": "manual", "amount": 100, "gift_id": None,
        "supplier_key": "manual", "stock": None,
        "pricing": {"source": "manual", "manual_amount": "100", "manual_currency": "XTR"}, "digital_in_telegram": True})
    await ts.sync_supply(fake_bot)
    await engine.refresh_all(None, fetch=False)
    return SimpleNamespace(gift=gift, prem=prem, stars=stars, bot=fake_bot, ts=ts)
