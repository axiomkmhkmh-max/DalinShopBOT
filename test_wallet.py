import pytest

from constants import has_permission
import wallet


async def test_credit_debit_and_ledger(db):
    await wallet.credit(1, 10000, idem_key="k1")
    await wallet.debit(1, 6000, idem_key="k2")
    assert await wallet.get_balance(1) == 4000
    assert (await wallet.reconcile(1))["ok"]


async def test_no_negative_balance(db):
    await wallet.credit(1, 5000, idem_key="k1")
    with pytest.raises(wallet.InsufficientFunds):
        await wallet.debit(1, 6000, idem_key="k2")
    assert await wallet.get_balance(1) == 5000


async def test_duplicate_transaction_rejected(db):
    await wallet.credit(1, 5000, idem_key="same")
    with pytest.raises(wallet.DuplicateTransaction):
        await wallet.credit(1, 5000, idem_key="same")
    assert await wallet.get_balance(1) == 5000


async def test_refund_only_once_per_order(db):
    await wallet.credit(1, 5000, idem_key="seed")
    await wallet.refund_order(1, 2000, "order-1", "finance1")
    with pytest.raises(wallet.DuplicateTransaction):
        await wallet.refund_order(1, 2000, "order-1", "finance1")
    assert await wallet.get_balance(1) == 7000
    assert (await wallet.reconcile(1))["ok"]


async def test_wallet_payment_is_idempotent_per_order(shop, sent):
    from payment_wallet import WalletProvider
    import orders, settings as st
    await st.set_setting("wallet_payment_enabled", "true")
    await wallet.credit(5, 500000, idem_key="seed")
    o = await orders.create_order(5, shop.pid, 1, "wallet")
    p = WalletProvider()
    paid = await p.pay(o)
    again = await p.pay(o)                         # replay must not debit twice
    assert paid["status"] == "PAID" and again is None
    assert await wallet.get_balance(5) == 500000 - o["total_toman"]


def test_support_cannot_change_wallets():
    assert not has_permission("support", "wallets_adjust")
    assert has_permission("finance", "wallets_adjust")
