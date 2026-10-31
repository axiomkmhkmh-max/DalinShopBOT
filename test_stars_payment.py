"""Telegram Stars flow tests (mock DB, no real payments, no real Telegram calls)."""
from datetime import timedelta

from constants import OrderStatus as S
from db import get_db
from payment_manager import available_methods, get_provider
import catalog, fulfillment, orders
from supplier_base import MockSupplier, register_override
from util import utcnow

stars = get_provider("stars")


async def _order(shop, user=1001):
    return await orders.create_order(user, shop.pid, 1, "stars")


async def _use_mock_supplier(shop, mode="deliver"):
    sup = MockSupplier(mode)
    register_override("mock", sup)
    await get_db().suppliers.update_one({"key": "mock"}, {"$set": {"name": "mock", "type": "mock", "enabled": True}}, upsert=True)
    await get_db().products.update_one({"public_id": shop.pid}, {"$set": {"supplier_key": "mock"}})
    return sup


async def test_order_exists_before_invoice_and_invoice_is_xtr(shop):
    o = await _order(shop)
    assert o["status"] == S.PENDING_PAYMENT and o["stars_amount"] == 103
    init = await stars.create_payment(o)
    assert init.kind == "telegram_invoice" and init.data["currency"] == "XTR"
    assert init.data["amount"] == 103 and init.data["payload"] == o["public_id"]


async def test_duplicate_order_creation_reuses_open_order(shop):
    a = await _order(shop)
    b = await _order(shop)
    assert a["public_id"] == b["public_id"]
    assert await get_db().orders.count_documents({}) == 1


async def test_successful_payment_then_delivery(shop, sent):
    sup = await _use_mock_supplier(shop)
    o = await _order(shop)
    ok, _ = await stars.pre_checkout(1001, o["public_id"], "XTR", 103)
    assert ok
    res = await stars.handle_successful_payment(1001, o["public_id"], "XTR", 103, "chg-1", "prov-1")
    assert res.code == "OK" and res.order["status"] == S.PAID
    assert res.order["paid_charge_id"] == "chg-1"
    assert sup.calls == 0                       # nothing delivered before the paid transition is processed
    await fulfillment.on_order_paid(res.order)
    final = await orders.get_order(o["public_id"])
    assert final["status"] == S.FULFILLED and sup.calls == 1
    pay = await get_db().payments.find_one({"_id": "stars:chg-1"})
    assert pay["status"] == "SUCCEEDED" and pay["amount"] == 103
    assert any("تحویل داده شد" in t for _, t in sent.users)


async def test_no_fulfillment_before_payment(shop):
    sup = await _use_mock_supplier(shop)
    o = await _order(shop)
    assert await fulfillment.fulfill_order(o["public_id"]) == "SKIPPED"
    assert sup.calls == 0
    assert (await orders.get_order(o["public_id"]))["status"] == S.PENDING_PAYMENT


async def test_pre_checkout_rejections(shop):
    o = await _order(shop)
    pid = o["public_id"]
    assert not (await stars.pre_checkout(1001, pid, "XTR", 102))[0]        # wrong amount
    assert not (await stars.pre_checkout(1001, pid, "USD", 103))[0]        # wrong currency
    assert not (await stars.pre_checkout(9999, pid, "XTR", 103))[0]        # someone else's order
    assert not (await stars.pre_checkout(1001, "nope", "XTR", 103))[0]     # unknown order
    await get_db().orders.update_one({"public_id": pid}, {"$set": {"expires_at": utcnow() - timedelta(minutes=1)}})
    assert not (await stars.pre_checkout(1001, pid, "XTR", 103))[0]        # expired


async def test_failed_payment_amount_mismatch_never_marks_paid(shop, sent):
    o = await _order(shop)
    res = await stars.handle_successful_payment(1001, o["public_id"], "XTR", 50, "chg-bad")
    assert res.code == "MISMATCH"
    cur = await orders.get_order(o["public_id"])
    assert cur["status"] == S.FAILED and cur["paid_at"] is None
    assert sent.admins                                   # admin is alerted
    assert (await get_db().payments.find_one({"_id": "stars:chg-bad"}))["status"] == "MISMATCH"


async def test_payment_for_unknown_order_is_recorded_not_fulfilled(shop, sent):
    res = await stars.handle_successful_payment(1001, "ghost", "XTR", 10, "chg-x")
    assert res.code == "UNKNOWN_ORDER" and sent.admins


async def test_duplicate_successful_payment_is_ignored(shop):
    sup = await _use_mock_supplier(shop)
    o = await _order(shop)
    first = await stars.handle_successful_payment(1001, o["public_id"], "XTR", 103, "chg-1")
    await fulfillment.on_order_paid(first.order)
    second = await stars.handle_successful_payment(1001, o["public_id"], "XTR", 103, "chg-1")
    assert second.code == "DUPLICATE"
    assert await get_db().payments.count_documents({"order_id": o["public_id"]}) == 1
    assert sup.calls == 1                                # delivered exactly once
    # a replayed fulfillment attempt is also a no-op
    assert await fulfillment.fulfill_order(o["public_id"]) == "SKIPPED"
    assert sup.calls == 1


async def test_late_payment_on_cancelled_order_flags_refund(shop, sent):
    o = await _order(shop)
    await orders.transition(o["public_id"], S.CANCELLED, from_states=[S.PENDING_PAYMENT])
    res = await stars.handle_successful_payment(1001, o["public_id"], "XTR", 103, "chg-late")
    assert res.code == "ORDER_CLOSED"
    assert (await get_db().payments.find_one({"_id": "stars:chg-late"}))["status"] == "NEEDS_REFUND"


async def test_delivery_failure_keeps_payment_and_can_retry_without_new_charge(shop, sent):
    sup = await _use_mock_supplier(shop, "fail")
    o = await _order(shop)
    res = await stars.handle_successful_payment(1001, o["public_id"], "XTR", 103, "chg-1")
    await fulfillment.on_order_paid(res.order)
    cur = await orders.get_order(o["public_id"])
    assert cur["status"] == S.FAILED and cur["paid_at"] is not None
    assert any("نیازی به پرداخت مجدد نیست" in t for _, t in sent.users)   # customer told not to pay again
    assert any(o["public_id"] in a for a in sent.admins)                   # admin gets the error
    assert await get_db().payments.count_documents({"order_id": o["public_id"]}) == 1
    sup.mode = "deliver"
    assert await fulfillment.fulfill_order(o["public_id"]) == "DELIVERED"
    assert (await orders.get_order(o["public_id"]))["status"] == S.FULFILLED
    assert (await get_db().supplier_orders.find_one({"order_id": o["public_id"]}))["attempts"] == 2


async def test_supplier_exception_is_handled(shop, sent):
    await _use_mock_supplier(shop, "raise")
    o = await _order(shop)
    res = await stars.handle_successful_payment(1001, o["public_id"], "XTR", 103, "chg-1")
    await fulfillment.on_order_paid(res.order)
    assert (await orders.get_order(o["public_id"]))["status"] == S.FAILED


async def test_manual_supplier_waits_for_admin(shop, sent):
    o = await _order(shop)
    res = await stars.handle_successful_payment(1001, o["public_id"], "XTR", 103, "chg-1")
    await fulfillment.on_order_paid(res.order)
    cur = await orders.get_order(o["public_id"])
    assert cur["status"] == S.PROCESSING and cur["fulfillment_status"] == "MANUAL_PENDING"
    assert await fulfillment.complete_manual(o["public_id"], "admin1", "کد: TEST")
    assert (await orders.get_order(o["public_id"]))["status"] == S.FULFILLED


async def test_ton_never_offered_for_digital_products(shop):
    product = await catalog.get_product(shop.pid)
    keys = [p.key for p in await available_methods(product)]
    assert keys == ["stars"]
    assert not await get_provider("ton").is_enabled()


async def test_mock_payment_never_offered_in_production(shop, monkeypatch):
    from types import SimpleNamespace
    import payment_mock as mock_mod
    monkeypatch.setattr(mock_mod, "get_config", lambda: SimpleNamespace(enable_mock_payment=True, is_production=True))
    assert not await get_provider("mock").is_enabled()
    monkeypatch.setattr(mock_mod, "get_config", lambda: SimpleNamespace(enable_mock_payment=True, is_production=False))
    assert await get_provider("mock").is_enabled()
