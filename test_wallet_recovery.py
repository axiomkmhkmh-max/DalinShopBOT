"""Crash recovery of wallet operations and orphan debits (simulated crashes; mock DB)."""
from datetime import timedelta

import pytest

import orders, payment_wallet, wallet
import settings as st
from constants import OrderStatus as S
from db import get_db
from payment_wallet import WalletProvider
from util import utcnow


async def test_crash_after_balance_change_before_ledger_completion_is_recovered(db, monkeypatch):
    await wallet.credit(1, 10_000, idem_key="seed")
    orig = db.wallet_ledger.update_one

    async def crash(*a, **k):                       # dies in step 3 (mark APPLIED)
        raise RuntimeError("process died")

    monkeypatch.setattr(db.wallet_ledger, "update_one", crash)
    with pytest.raises(RuntimeError):
        await wallet.debit(1, 4_000, idem_key="op1")
    monkeypatch.setattr(db.wallet_ledger, "update_one", orig)
    assert await wallet.get_balance(1) == 6_000                                  # money moved exactly once
    await db.wallet_ledger.update_one({"idem_key": "op1"}, {"$set": {"created_at": utcnow() - timedelta(minutes=10)}})
    assert (await wallet.recover_pending())["completed"] == 1
    assert (await wallet.reconcile(1))["ok"] and await wallet.get_balance(1) == 6_000


async def test_retry_after_crash_resumes_without_double_charge(db, monkeypatch):
    await wallet.credit(1, 10_000, idem_key="seed")
    orig = db.wallet_ledger.update_one
    monkeypatch.setattr(db.wallet_ledger, "update_one", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("died")))
    with pytest.raises(RuntimeError):
        await wallet.debit(1, 4_000, idem_key="op1")
    monkeypatch.setattr(db.wallet_ledger, "update_one", orig)
    with pytest.raises(wallet.DuplicateTransaction):                              # same key again: finished, not repeated
        await wallet.debit(1, 4_000, idem_key="op1")
    assert await wallet.get_balance(1) == 6_000 and (await wallet.reconcile(1))["ok"]


async def test_pending_row_that_never_touched_the_balance_is_dropped(db):
    await db.wallets.insert_one({"user_id": 2, "balance": 500, "applied_keys": []})
    await db.wallet_ledger.insert_one({"idem_key": "ghost", "user_id": 2, "type": "DEBIT", "amount": -100, "status": "PENDING",
                                       "created_at": utcnow() - timedelta(minutes=10)})
    assert (await wallet.recover_pending())["dropped"] == 1
    assert await wallet.get_balance(2) == 500 and (await wallet.reconcile(2))["ok"]


async def test_insufficient_funds_leaves_no_ledger_trace(db):
    await wallet.credit(1, 100, idem_key="seed")
    with pytest.raises(wallet.InsufficientFunds):
        await wallet.debit(1, 500, idem_key="big")
    assert await db.wallet_ledger.count_documents({"idem_key": "big"}) == 0
    await wallet.credit(1, 1_000, idem_key="more")
    await wallet.debit(1, 500, idem_key="big")                                    # the same key can still be used later
    assert await wallet.get_balance(1) == 600


async def test_concurrent_debits_never_go_negative(db):
    import asyncio
    await wallet.credit(1, 1_000, idem_key="seed")
    res = await asyncio.gather(*[wallet.debit(1, 400, idem_key=f"d{i}") for i in range(5)], return_exceptions=True)
    assert sum(1 for r in res if isinstance(r, dict)) == 2 and await wallet.get_balance(1) == 200
    assert (await wallet.reconcile(1))["ok"]


async def test_orphan_debit_of_an_unpaid_closed_order_is_given_back_once(tg, sent):
    await st.set_setting("wallet_payment_enabled", "true")
    await wallet.credit(5, 1_000_000, idem_key="seed")
    o = await orders.create_order(5, tg.stars, 1, "wallet", target_username="@Bob_99")
    await wallet.debit(5, o["total_toman"], idem_key=f"order:{o['public_id']}", kind="PURCHASE", ref=o["public_id"])   # crash before PAID
    await orders.transition(o["public_id"], S.CANCELLED, from_states=[S.PENDING_PAYMENT], note="expired")
    assert await payment_wallet.recover_orphan_debits() == 1
    assert await payment_wallet.recover_orphan_debits() == 0
    assert await wallet.get_balance(5) == 1_000_000 and (await wallet.reconcile(5))["ok"]


async def test_paid_orders_are_never_reverted(tg, sent):
    await st.set_setting("wallet_payment_enabled", "true")
    await wallet.credit(5, 1_000_000, idem_key="seed")
    o = await orders.create_order(5, tg.stars, 1, "wallet", target_username="@Bob_99")
    assert await WalletProvider().pay(o)
    assert await payment_wallet.recover_orphan_debits() == 0
    assert await wallet.get_balance(5) == 1_000_000 - o["total_toman"]
