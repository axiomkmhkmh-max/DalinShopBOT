"""Wallet-paid Stars orders: destination account, insufficient balance, duplicate payment, refund, duplicate delivery.
Fake bot / mock DB / TEST-ONLY Stars sender: no real Telegram, no real money."""
import asyncio
from datetime import timedelta

import pytest

import fulfillment, orders, wallet
import settings as st
from constants import OrderStatus as S, canonical_status
from db import get_db
from payment_wallet import WalletProvider
from stars_delivery import StarsSendRefused
from util import utcnow


@pytest.fixture
async def funded(tg, sent):
    await st.set_setting("wallet_payment_enabled", "true")
    await wallet.credit(5, 1_000_000, idem_key="seed")
    return tg


async def _order(tg, target="@Bob_99", user=5, qty=1):
    return await orders.create_order(user, tg.stars, qty, "wallet", target_username=target)


# ------------------------------------------------------------------ destination account
async def test_order_stores_normalized_destination_and_it_is_in_the_duplicate_key(tg):
    a = await _order(tg, "@Bob_99")
    same = await _order(tg, "t.me/bob_99")                 # same account (case-insensitive) -> same open order
    other = await _order(tg, "@Alice_1")
    assert a["target_username"] == "Bob_99" and same["public_id"] == a["public_id"]
    assert other["public_id"] != a["public_id"] and other["target_username"] == "Alice_1"


@pytest.mark.parametrize("bad", ["@abc", "12345", "@bad name", "@_x_y_z", "@" + "a" * 40])
async def test_invalid_destination_is_rejected_before_any_order(tg, bad):
    with pytest.raises(orders.OrderError):
        await _order(tg, bad)
    assert await get_db().orders.count_documents({}) == 0


async def test_destination_is_ignored_for_non_stars_products(tg):
    o = await orders.create_order(5, tg.gift, 1, "stars", target_username="@Bob_99")
    assert o["target_username"] is None


async def test_manual_delivery_alert_names_the_destination(funded, sent):
    o = await _order(funded)
    paid = await WalletProvider().pay(o)
    assert await fulfillment.on_order_paid(paid) == "PENDING"
    cur = await orders.get_order(o["public_id"])
    assert cur["status"] == S.PROCESSING and canonical_status(cur) == "pending_manual_delivery"
    assert any("@Bob_99" in a and o["public_id"] in a for a in sent.admins)


async def test_sender_receives_the_destination_account(funded, stars_sender, sent):
    o = await _order(funded)
    paid = await WalletProvider().pay(o)
    assert await fulfillment.on_order_paid(paid) == "DELIVERED"
    assert stars_sender.sent == [(5, 100, f"order:{o['public_id']}")] and stars_sender.targets == ["Bob_99"]


# ------------------------------------------------------------------ payment safety
async def test_insufficient_balance_charges_nothing(tg, sent):
    await st.set_setting("wallet_payment_enabled", "true")
    await wallet.credit(5, 100, idem_key="seed")
    o = await _order(tg)
    with pytest.raises(wallet.InsufficientFunds):
        await WalletProvider().pay(o)
    assert await wallet.get_balance(5) == 100
    assert (await orders.get_order(o["public_id"]))["status"] == S.PENDING_PAYMENT
    assert await get_db().wallet_ledger.count_documents({"type": "PURCHASE"}) == 0


async def test_duplicate_and_concurrent_payment_debits_once(funded):
    o = await _order(funded)
    p = WalletProvider()
    results = await asyncio.gather(p.pay(o), p.pay(o), p.pay(o))
    assert sum(1 for r in results if r) >= 1
    assert await wallet.get_balance(5) == 1_000_000 - o["total_toman"]
    assert await get_db().wallet_ledger.count_documents({"type": "PURCHASE"}) == 1
    assert (await wallet.reconcile(5))["ok"]


async def test_expired_order_is_never_charged(funded):
    o = await _order(funded)
    await get_db().orders.update_one({"public_id": o["public_id"]}, {"$set": {"expires_at": utcnow() - timedelta(minutes=1)}})
    assert await WalletProvider().pay(await orders.get_order(o["public_id"])) is None
    assert await wallet.get_balance(5) == 1_000_000


async def test_order_closed_during_payment_gets_its_money_back(funded, monkeypatch):
    o = await _order(funded)
    original = orders.transition

    async def cancel_first(pid, to, **kw):
        if to == S.PAID:
            await original(pid, S.CANCELLED, from_states=[S.PENDING_PAYMENT], note="race")
        return await original(pid, to, **kw)

    monkeypatch.setattr(orders, "transition", cancel_first)
    assert await WalletProvider().pay(o) is None
    assert await wallet.get_balance(5) == 1_000_000 and (await wallet.reconcile(5))["ok"]


async def test_disabled_wallet_payment_is_refused(tg):
    from payment_base import ProviderDisabled
    o = await _order(tg)
    with pytest.raises(ProviderDisabled):
        await WalletProvider().pay(o)


# ------------------------------------------------------------------ refunds
async def _paid_and_pending(funded):
    o = await _order(funded)
    paid = await WalletProvider().pay(o)
    await fulfillment.on_order_paid(paid)
    return o


async def test_wallet_refund_returns_money_once_and_records_reason(funded):
    o = await _paid_and_pending(funded)
    cur = await orders.get_order(o["public_id"])
    assert await WalletProvider().refund(cur, actor="finance1", reason="مشتری انصراف داد")
    assert await wallet.get_balance(5) == 1_000_000 and (await wallet.reconcile(5))["ok"]
    done = await orders.get_order(o["public_id"])
    assert done["status"] == S.REFUNDED and done["payment_status"] == "REFUNDED" and done["refund_reason"] == "مشتری انصراف داد"
    assert not await WalletProvider().refund(done, actor="finance1", reason="again")
    assert await wallet.get_balance(5) == 1_000_000


async def test_concurrent_refunds_credit_once(funded):
    o = await _paid_and_pending(funded)
    cur = await orders.get_order(o["public_id"])
    results = await asyncio.gather(*[WalletProvider().refund(cur, actor="a", reason="r") for _ in range(3)])
    assert sum(results) == 1 and await wallet.get_balance(5) == 1_000_000
    assert await get_db().wallet_ledger.count_documents({"type": "REFUND"}) == 1


async def test_refund_needs_a_reason_and_a_wallet_payment(funded):
    o = await _paid_and_pending(funded)
    cur = await orders.get_order(o["public_id"])
    assert not await WalletProvider().refund(cur, actor="a", reason="  ")
    assert await wallet.get_balance(5) == 1_000_000 - o["total_toman"]
    stars_paid = {**cur, "payment_method": "stars"}
    assert not await WalletProvider().refund(stars_paid, actor="a", reason="x")


async def test_unpaid_order_cannot_be_refunded(funded):
    o = await _order(funded)
    assert not await WalletProvider().refund(o, actor="a", reason="x")
    assert await wallet.get_balance(5) == 1_000_000


# ------------------------------------------------------------------ delivery safety
async def test_delivery_runs_once_even_if_triggered_again(funded, stars_sender):
    o = await _order(funded)
    paid = await WalletProvider().pay(o)
    assert await fulfillment.on_order_paid(paid) == "DELIVERED"
    assert await fulfillment.fulfill_order(o["public_id"]) == "SKIPPED"
    results = await asyncio.gather(*[fulfillment.fulfill_order(o["public_id"]) for _ in range(3)])
    assert set(results) == {"SKIPPED"} and len(stars_sender.sent) == 1


async def test_manual_delivery_is_recorded_once_with_tracking_id(funded, sent):
    o = await _paid_and_pending(funded)
    assert await fulfillment.complete_manual(o["public_id"], "admin1", "TRK-123")
    assert not await fulfillment.complete_manual(o["public_id"], "admin1", "TRK-999")
    cur = await orders.get_order(o["public_id"])
    assert cur["status"] == S.FULFILLED and cur["delivery_ref"] == "TRK-123" and canonical_status(cur) == "delivered"
    assert sum(1 for _, text in sent.users if "تحویل داده شد" in text) == 1


async def test_auto_refund_only_when_failure_is_certain_and_enabled(funded, stars_sender, sent):
    await st.set_setting("wallet_auto_refund_on_fail", "true")
    stars_sender.fail = StarsSendRefused("account refused")           # SURE nothing was sent
    o = await _order(funded)
    assert await fulfillment.on_order_paid(await WalletProvider().pay(o)) == "FAILED"
    cur = await orders.get_order(o["public_id"])
    assert cur["status"] == S.REFUNDED and await wallet.get_balance(5) == 1_000_000
    assert any("بازگشت" in t or "برگشت" in t for _, t in sent.users)


async def test_unknown_outcome_is_never_auto_refunded(funded, stars_sender):
    await st.set_setting("wallet_auto_refund_on_fail", "true")
    stars_sender.fail = RuntimeError("connection reset")              # outcome UNKNOWN: money may have moved
    o = await _order(funded)
    assert await fulfillment.on_order_paid(await WalletProvider().pay(o)) == "PENDING"
    cur = await orders.get_order(o["public_id"])
    assert cur["status"] == S.PROCESSING and await wallet.get_balance(5) == 1_000_000 - o["total_toman"]


async def test_auto_refund_is_off_by_default(funded, stars_sender):
    stars_sender.fail = StarsSendRefused("refused")
    o = await _order(funded)
    assert await fulfillment.on_order_paid(await WalletProvider().pay(o)) == "FAILED"
    assert (await orders.get_order(o["public_id"]))["status"] == S.FAILED
    assert await wallet.get_balance(5) == 1_000_000 - o["total_toman"]


def test_canonical_status_names():
    cases = [({"status": S.PENDING_PAYMENT}, "awaiting_payment"), ({"status": S.PAID}, "paid"),
             ({"status": S.PROCESSING, "fulfillment_status": "PROCESSING"}, "processing"),
             ({"status": S.PROCESSING, "fulfillment_status": "MANUAL_PENDING"}, "pending_manual_delivery"),
             ({"status": S.FULFILLED}, "delivered"), ({"status": S.FAILED}, "failed"), ({"status": S.REFUNDED}, "refunded")]
    for order, name in cases:
        assert canonical_status(order) == name
