"""Stars supplier flow with a MOCK supplier (scriptable fake adapter). These tests prove OUR logic: statuses, retries,
refunds, duplicate protection, concurrency. They do NOT prove that real Stars were delivered by a real supplier."""
import asyncio
from datetime import timedelta
from types import SimpleNamespace

import pytest

import fulfillment, orders, supplier_tracking as tr, wallet
import settings as st
import stars_supplier as sup
from constants import OrderStatus as S, canonical_status
from db import get_db
from payment_wallet import WalletProvider
from stars_supplier import (NormalizedStatus as N, StarsSupplierAdapter, SupplierAuthError, SupplierOrderInfo, SupplierQuote,
                            SupplierRejected, SupplierTransient)
from util import utcnow

RAW = {"pending": N.ACCEPTED, "processing": N.PROCESSING, "completed": N.DELIVERED, "failed": N.FAILED,
       "cancelled": N.FAILED, "refunded": N.FAILED, "weird": N.UNKNOWN}


class MockSupplier(StarsSupplierAdapter):
    name, min_stars, max_stars = "mock", 50, 1_000_000

    def __init__(self, unit=1500):
        self.unit, self.orders, self.by_key = unit, {}, {}
        self.create_calls, self.get_calls = [], []
        self.create_script, self.get_script = [], []
        self.quote_error = None

    async def ping(self):
        return {"ok": True, "environment": "test"}

    async def balance(self):
        return 10 ** 9

    async def quote(self, quantity):
        if self.quote_error:
            raise self.quote_error
        return SupplierQuote(quantity * self.unit, "qt_1")

    async def create_order(self, *, idempotency_key, quantity, recipient, metadata=None):
        self.create_calls.append((idempotency_key, quantity, recipient))
        script = self.create_script.pop(0) if self.create_script else None
        if isinstance(script, Exception):
            raise script
        if idempotency_key not in self.by_key:                 # like the real API: same key = same order
            ref = f"ord_test_{len(self.orders) + 1}"
            self.orders[ref] = {"status": "pending", "quantity": quantity, "recipient": recipient}
            self.by_key[idempotency_key] = ref
        if script == "lose":                                   # supplier created it, the answer never arrived
            raise SupplierTransient("response lost", code="network_error")
        return SupplierOrderInfo(self.by_key[idempotency_key], N.ACCEPTED, "pending")

    async def get_order(self, ref):
        self.get_calls.append(ref)
        if self.get_script:
            s = self.get_script.pop(0)
            if isinstance(s, Exception):
                raise s
        raw = self.orders[ref]["status"]
        return SupplierOrderInfo(ref, RAW[raw], raw, failure_reason="supplier says no" if RAW[raw] is N.FAILED else "")

    def set_all(self, raw):
        for o in self.orders.values():
            o["status"] = raw


@pytest.fixture
async def shop(tg, sent, monkeypatch):
    supplier = MockSupplier()
    sup.register_adapter(supplier)
    monkeypatch.setattr(tr, "POLL_PAUSE", 0)
    monkeypatch.setattr(tr, "_cfg", lambda: SimpleNamespace(stars_supplier_poll_interval=20, stars_supplier_max_retries=3,
                                                            stars_supplier_stuck_minutes=120))
    await st.set_setting("wallet_payment_enabled", "true")
    await wallet.credit(5, 1_000_000, idem_key="seed")
    yield SimpleNamespace(tg=tg, supplier=supplier, sent=sent)
    sup.register_adapter(None)


async def place(shop, user=5, target="@Bob_99", qty=1):
    if user != 5 and await wallet.get_balance(user) == 0:
        await wallet.credit(user, 1_000_000, idem_key=f"seed{user}")
    o = await orders.create_order(user, shop.tg.stars, qty, "wallet", target_username=target)
    paid = await WalletProvider().pay(o)
    return o, await fulfillment.on_order_paid(paid)


async def due():
    await get_db().supplier_orders.update_many({}, {"$set": {"next_poll_at": utcnow() - timedelta(seconds=1)}})


async def row(oid):
    return await get_db().supplier_orders.find_one({"order_id": oid})


async def cur(oid):
    return await orders.get_order(oid)


# ------------------------------------------------------------------ happy path
async def test_paid_order_is_sent_to_supplier_with_destination_and_stable_key(shop):
    o, res = await place(shop)
    assert res == "SUPPLIER_PENDING"
    assert shop.supplier.create_calls == [(f"shop-{o['public_id']}", 100, "Bob_99")]
    c = await cur(o["public_id"])
    assert c["status"] == S.PROCESSING and canonical_status(c) == "supplier_pending"
    r = await row(o["public_id"])
    assert r["external_ref"] == "ord_test_1" and r["track_state"] == tr.SUBMITTED and r["supplier_status"] == "pending"


async def test_processing_then_delivered_completes_the_order_once(shop):
    o, _ = await place(shop)
    shop.supplier.set_all("processing")
    await due(); await tr.run_poll_cycle()
    assert canonical_status(await cur(o["public_id"])) == "supplier_pending" and (await row(o["public_id"]))["supplier_status"] == "processing"
    shop.supplier.set_all("completed")
    await due(); await tr.run_poll_cycle()
    c = await cur(o["public_id"])
    assert c["status"] == S.FULFILLED and canonical_status(c) == "delivered"
    await due(); await tr.run_poll_cycle()                                       # nothing left to do
    assert sum(1 for _, t in shop.sent.users if "تحویل داده شد" in t) == 1


async def test_customer_is_told_once_when_the_order_is_accepted(shop):
    await place(shop)
    assert sum(1 for _, t in shop.sent.users if "در حال پردازش" in t) == 1


# ------------------------------------------------------------------ definite failure + refund
@pytest.mark.parametrize("final", ["failed", "cancelled", "refunded"])
async def test_definite_failure_refunds_the_wallet_exactly_once(shop, final):
    o, _ = await place(shop)
    shop.supplier.set_all(final)
    await due(); await tr.run_poll_cycle()
    c = await cur(o["public_id"])
    assert c["status"] == S.REFUNDED and c["payment_status"] == "REFUNDED"
    assert await wallet.get_balance(5) == 1_000_000 and (await wallet.reconcile(5))["ok"]
    await due(); await tr.run_poll_cycle()
    assert await get_db().wallet_ledger.count_documents({"type": "REFUND"}) == 1
    assert (await row(o["public_id"]))["track_state"] == tr.FAILED_DEFINITE


async def test_definite_rejection_at_submit_refunds_immediately_and_creates_nothing(shop):
    shop.supplier.create_script = [SupplierRejected("bad recipient", code="recipient_invalid", http_status=400)]
    o, res = await place(shop)
    assert res == "FAILED" and shop.supplier.orders == {}
    assert (await cur(o["public_id"]))["status"] == S.REFUNDED and await wallet.get_balance(5) == 1_000_000


async def test_failure_handler_cannot_refund_twice_even_if_called_again_or_by_admin(shop):
    o, _ = await place(shop)
    shop.supplier.set_all("failed")
    await due(); await tr.run_poll_cycle()
    await fulfillment.finish_failed(o["public_id"], "again", refund=True)
    assert not await WalletProvider().refund(await cur(o["public_id"]), actor="admin", reason="x")
    assert await wallet.get_balance(5) == 1_000_000
    assert await get_db().wallet_ledger.count_documents({"type": "REFUND"}) == 1


# ------------------------------------------------------------------ timeouts / unknown outcomes
async def test_timeout_when_submitting_is_not_a_failure_and_never_refunds(shop):
    shop.supplier.create_script = [SupplierTransient("timeout", code="network_error")]
    o, res = await place(shop)
    assert res == "SUPPLIER_PENDING"
    r = await row(o["public_id"])
    assert r["track_state"] == tr.TO_SUBMIT and r["fail_count"] == 1 and not r.get("external_ref")
    c = await cur(o["public_id"])
    assert c["status"] == S.PROCESSING and await wallet.get_balance(5) == 1_000_000 - o["total_toman"]
    assert not any("برگشت" in t for _, t in shop.sent.users)


async def test_response_lost_after_supplier_created_the_order_does_not_create_a_second_one(shop):
    shop.supplier.create_script = ["lose"]
    o, _ = await place(shop)
    assert len(shop.supplier.orders) == 1
    await due(); await tr.run_poll_cycle()                                          # worker re-sends with the SAME key
    assert len(shop.supplier.orders) == 1 and len({k for k, _, _ in shop.supplier.create_calls}) == 1
    r = await row(o["public_id"])
    assert r["track_state"] == tr.SUBMITTED and r["external_ref"] == "ord_test_1"


async def test_unknown_submit_outcome_after_max_retries_goes_to_manual_review_without_refund(shop):
    shop.supplier.create_script = [SupplierTransient("t", code="network_error")] * 3     # exactly the 3 allowed attempts
    o, _ = await place(shop)
    for _ in range(3):
        await due(); await tr.run_poll_cycle()
    c = await cur(o["public_id"])
    assert canonical_status(c) == "manual_review" and c["status"] == S.PROCESSING
    assert await wallet.get_balance(5) == 1_000_000 - o["total_toman"]
    assert any(o["public_id"] in a and "بررسی دستی" in a for a in shop.sent.admins)
    assert await tr.retry_submit(o["public_id"])                                    # admin: send again (same key)
    await due(); await tr.run_poll_cycle()
    assert (await row(o["public_id"]))["track_state"] == tr.SUBMITTED and len(shop.supplier.orders) == 1


async def test_timeouts_while_polling_never_fail_the_order_and_recover(shop):
    o, _ = await place(shop)
    shop.supplier.get_script = [SupplierTransient("t", code="network_error")] * 2
    for _ in range(2):
        await due(); await tr.run_poll_cycle()
    r = await row(o["public_id"])
    assert r["track_state"] == tr.SUBMITTED and r["fail_count"] == 2 and (await cur(o["public_id"]))["status"] == S.PROCESSING
    shop.supplier.set_all("completed")
    await due(); await tr.run_poll_cycle()
    assert (await cur(o["public_id"]))["status"] == S.FULFILLED


async def test_repeated_poll_errors_go_to_manual_review_and_late_delivery_still_completes(shop):
    o, _ = await place(shop)
    shop.supplier.get_script = [SupplierTransient("t", code="network_error")] * 3
    for _ in range(3):
        await due(); await tr.run_poll_cycle()
    assert canonical_status(await cur(o["public_id"])) == "manual_review"
    assert await wallet.get_balance(5) == 1_000_000 - o["total_toman"]
    shop.supplier.set_all("completed")
    await due(); await tr.run_poll_cycle()                                          # reviewed orders keep being checked
    assert (await cur(o["public_id"]))["status"] == S.FULFILLED


async def test_unknown_supplier_status_is_never_a_failure(shop):
    o, _ = await place(shop)
    shop.supplier.set_all("weird")
    for _ in range(4):
        await due(); await tr.run_poll_cycle()
    assert (await cur(o["public_id"]))["status"] == S.PROCESSING and await wallet.get_balance(5) < 1_000_000
    await due(); await tr.run_poll_cycle()                                          # 5th unknown answer
    assert canonical_status(await cur(o["public_id"])) == "manual_review"


async def test_order_stuck_at_supplier_is_flagged_not_refunded(shop):
    o, _ = await place(shop)
    await get_db().supplier_orders.update_one({"order_id": o["public_id"]}, {"$set": {"submitted_at": utcnow() - timedelta(hours=3)}})
    await due(); await tr.run_poll_cycle()
    assert canonical_status(await cur(o["public_id"])) == "manual_review"
    assert await wallet.get_balance(5) == 1_000_000 - o["total_toman"]


async def test_key_or_supplier_wallet_problem_goes_to_manual_review_not_refund(shop):
    shop.supplier.create_script = [SupplierAuthError("no funds", code="insufficient_funds", http_status=402)]
    o, res = await place(shop)
    assert res == "MANUAL_REVIEW" and canonical_status(await cur(o["public_id"])) == "manual_review"
    assert await wallet.get_balance(5) == 1_000_000 - o["total_toman"] and shop.supplier.orders == {}


# ------------------------------------------------------------------ duplicates / concurrency
async def test_fulfillment_triggered_many_times_creates_one_supplier_order(shop):
    o, _ = await place(shop)
    results = await asyncio.gather(*[fulfillment.fulfill_order(o["public_id"]) for _ in range(4)])
    assert set(results) == {"SKIPPED"} and len(shop.supplier.orders) == 1 and len(shop.supplier.create_calls) == 1


async def test_paying_twice_debits_and_sends_once(shop):
    o = await orders.create_order(5, shop.tg.stars, 1, "wallet", target_username="@Bob_99")
    paid = await asyncio.gather(*[WalletProvider().pay(o) for _ in range(3)])
    for p in paid:
        if p:
            await fulfillment.on_order_paid(p)
    assert await wallet.get_balance(5) == 1_000_000 - o["total_toman"] and len(shop.supplier.orders) == 1


async def test_several_orders_at_the_same_time_each_get_their_own_supplier_order(shop):
    async def one(i):
        return await place(shop, user=100 + i, target=f"@user_{i}x1")
    placed = await asyncio.gather(*[one(i) for i in range(5)])
    assert len(shop.supplier.orders) == 5 and len({k for k, _, _ in shop.supplier.create_calls}) == 5
    assert {r for _, _, r in shop.supplier.create_calls} == {f"user_{i}x1" for i in range(5)}
    shop.supplier.set_all("completed")
    await due(); await tr.run_poll_cycle()
    for o, _ in placed:
        assert (await cur(o["public_id"]))["status"] == S.FULFILLED


async def test_two_workers_never_check_the_same_order_at_once(shop):
    o, _ = await place(shop)
    await due()
    await asyncio.gather(tr.run_poll_cycle(), tr.run_poll_cycle())
    assert len(shop.supplier.get_calls) == 1


async def test_worker_finishing_twice_notifies_once(shop):
    o, _ = await place(shop)
    shop.supplier.set_all("completed")
    await due(); await asyncio.gather(tr.run_poll_cycle(), tr.run_poll_cycle())
    assert sum(1 for _, t in shop.sent.users if "تحویل داده شد" in t) == 1


# ------------------------------------------------------------------ before the customer pays
async def test_unreachable_supplier_blocks_new_orders_without_creating_them(shop):
    shop.supplier.quote_error = SupplierTransient("down", code="network_error")
    with pytest.raises(orders.OrderError):
        await orders.create_order(5, shop.tg.stars, 1, "wallet", target_username="@Bob_99")
    assert await get_db().orders.count_documents({}) == 0


async def test_supplier_price_above_sale_price_blocks_new_orders(shop):
    shop.supplier.unit = 5000                                   # 100 Stars would cost 500000 > sale price
    with pytest.raises(orders.OrderError):
        await orders.create_order(5, shop.tg.stars, 1, "wallet", target_username="@Bob_99")


async def test_insufficient_customer_balance_never_reaches_the_supplier(shop):
    await wallet.credit(8, 100, idem_key="small")
    o = await orders.create_order(8, shop.tg.stars, 1, "wallet", target_username="@Bob_99")
    with pytest.raises(wallet.InsufficientFunds):
        await WalletProvider().pay(o)
    assert shop.supplier.create_calls == [] and await wallet.get_balance(8) == 100


async def test_order_without_destination_is_never_sent(shop):
    o, res = await place(shop, target=None)
    assert res == "MANUAL_REVIEW" and shop.supplier.create_calls == []


# ------------------------------------------------------------------ manual handling & safety
async def test_manual_delivery_after_review_closes_tracking_and_blocks_second_delivery(shop):
    shop.supplier.create_script = [SupplierAuthError("no funds", code="insufficient_funds", http_status=402)]
    o, _ = await place(shop)
    assert await fulfillment.complete_manual(o["public_id"], "admin", "TRK-1")
    assert not await fulfillment.complete_manual(o["public_id"], "admin", "TRK-2")
    assert (await cur(o["public_id"]))["status"] == S.FULFILLED


async def test_refund_by_admin_then_supplier_delivers_alerts_the_admins(shop):
    o, _ = await place(shop)
    assert await WalletProvider().refund(await cur(o["public_id"]), actor="admin", reason="customer asked")
    shop.supplier.set_all("completed")
    await due(); await tr.run_poll_cycle()
    assert any("تحویل‌شده اعلام می‌کند" in a or "تأیید کرد" in a for a in shop.sent.admins)
    assert (await row(o["public_id"]))["track_state"] == tr.CLOSED and await wallet.get_balance(5) == 1_000_000


async def test_errors_stored_for_admin_are_redacted(shop, monkeypatch):
    monkeypatch.setattr(tr, "redact", lambda t: str(t).replace("sk_test_SECRETKEY123", "***"))
    shop.supplier.create_script = [SupplierTransient("boom sk_test_SECRETKEY123", code="network_error")]
    o, _ = await place(shop)
    assert "SECRETKEY" not in (await row(o["public_id"]))["last_error"]


async def test_supplier_disabled_keeps_the_old_manual_flow(tg, sent):
    sup.register_adapter(None)
    await st.set_setting("wallet_payment_enabled", "true")
    await wallet.credit(5, 1_000_000, idem_key="seed")
    o = await orders.create_order(5, tg.stars, 1, "wallet", target_username="@Bob_99")
    assert await fulfillment.on_order_paid(await WalletProvider().pay(o)) == "PENDING"      # manual, as before
