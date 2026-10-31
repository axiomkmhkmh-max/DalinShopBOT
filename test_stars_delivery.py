"""Automatic Stars delivery rules (>= 50 Stars) with a TEST-ONLY sender. NOT a real Telegram/bank test:
no official way to send Stars exists, so production registers no sender and every order stays manual."""
import pytest

from constants import OrderStatus as S
from db import get_db
from payment_manager import get_provider
import catalog, fulfillment, orders, stars_delivery
import settings as st
from stars_delivery import StarsSendRefused
from util import utcnow

stars = get_provider("stars")


async def _stars_product(tg, amount):
    return await catalog.save_product({
        "name": f"{amount} Stars", "kind": "stars", "enabled": True, "delivery": "stars_auto", "amount": amount,
        "gift_id": None, "supplier_key": "stars", "stock": None,
        "pricing": {"source": "manual", "manual_amount": str(amount), "manual_currency": "XTR"}, "digital_in_telegram": True})


async def _pay(pid, qty=1, user=1001, charge="c1"):
    from payment_stars import StarsProvider
    o = await orders.create_order(user, pid, qty, "stars")
    res = await stars.handle_successful_payment(user, o["public_id"], "XTR", o["stars_amount"], charge)
    assert res.code == "OK"
    return o, res.order


async def _prep(tg, amount):
    pid = await _stars_product(tg, amount)
    from pricing_engine import refresh_all
    await refresh_all(None, fetch=False)
    return pid


# ------------------------------------------------------------------ production default: no sender => manual
async def test_no_sender_registered_means_manual_even_for_big_orders(tg, sent):
    assert stars_delivery.get_sender() is None
    pid = await _prep(tg, 500)
    o, paid = await _pay(pid)
    assert await fulfillment.on_order_paid(paid) == "PENDING"
    cur = await orders.get_order(o["public_id"])
    assert cur["status"] == S.PROCESSING and cur["fulfillment_status"] == "MANUAL_PENDING"
    assert "روش رسمی" in cur["manual_reason"]
    assert any(o["public_id"] in a for a in sent.admins)


# ------------------------------------------------------------------ threshold
@pytest.mark.parametrize("amount,auto", [(25, False), (49, False), (50, True), (75, True), (100, True), (250, True), (500, True)])
async def test_threshold_50(tg, sent, stars_sender, amount, auto):
    pid = await _prep(tg, amount)
    o, paid = await _pay(pid)
    result = await fulfillment.on_order_paid(paid)
    cur = await orders.get_order(o["public_id"])
    if auto:
        assert result == "DELIVERED" and cur["status"] == S.FULFILLED
        assert stars_sender.sent == [(1001, amount, f"order:{o['public_id']}")]
        assert cur["completed_at"] is not None
    else:
        assert result == "PENDING" and cur["fulfillment_status"] == "MANUAL_PENDING" and stars_sender.sent == []
        assert "کمتر از 50" in cur["manual_reason"]


async def test_threshold_uses_total_of_quantity(tg, sent, stars_sender):
    pid = await _prep(tg, 25)
    o, paid = await _pay(pid, qty=2)               # 2 x 25 = 50
    assert await fulfillment.on_order_paid(paid) == "DELIVERED"
    assert stars_sender.sent[0][1] == 50           # one send of the total


async def test_threshold_is_an_admin_setting(tg, sent, stars_sender):
    await st.set_setting("stars_auto_min", "100")
    pid = await _prep(tg, 75)
    o, paid = await _pay(pid)
    assert await fulfillment.on_order_paid(paid) == "PENDING"


# ------------------------------------------------------------------ never before verified payment
async def test_no_send_before_payment(tg, stars_sender):
    pid = await _prep(tg, 100)
    o = await orders.create_order(1, pid, 1, "stars")
    assert await fulfillment.fulfill_order(o["public_id"]) == "SKIPPED"
    assert stars_sender.sent == []
    assert (await orders.get_order(o["public_id"]))["status"] == S.PENDING_PAYMENT


async def test_wrong_amount_payment_is_never_delivered(tg, stars_sender, sent):
    pid = await _prep(tg, 100)
    o = await orders.create_order(1, pid, 1, "stars")
    res = await stars.handle_successful_payment(1, o["public_id"], "XTR", 1, "bad")
    assert res.code == "MISMATCH"
    assert stars_sender.sent == []


# ------------------------------------------------------------------ balance
async def test_insufficient_sender_balance_blocks_order_before_payment(tg, stars_sender):
    pid = await _prep(tg, 100)
    stars_sender._balance = 99
    with pytest.raises(orders.OrderError, match="موجودی"):
        await orders.create_order(1, pid, 1, "stars")
    assert await get_db().orders.count_documents({}) == 0


async def test_balance_is_rechecked_before_sending(tg, stars_sender, sent):
    pid = await _prep(tg, 100)
    o, paid = await _pay(pid)
    stars_sender._balance = 10                      # drained between order and delivery
    assert await fulfillment.on_order_paid(paid) == "FAILED"
    assert stars_sender.sent == [] and (await orders.get_order(o["public_id"]))["status"] == S.FAILED
    assert any("نیازی به پرداخت مجدد نیست" in t for _, t in sent.users)
    stars_sender._balance = 1000                    # admin tops up, retry
    assert await fulfillment.fulfill_order(o["public_id"]) == "DELIVERED"
    assert len(stars_sender.sent) == 1


async def test_small_orders_are_not_blocked_by_sender_balance(tg, stars_sender):
    pid = await _prep(tg, 25)
    stars_sender._balance = 0
    assert (await orders.create_order(1, pid, 1, "stars"))["status"] == S.PENDING_PAYMENT


# ------------------------------------------------------------------ failures and duplicates
async def test_refused_send_fails_order_and_retry_sends_once(tg, stars_sender, sent):
    pid = await _prep(tg, 100)
    stars_sender.fail = StarsSendRefused("user blocked")
    o, paid = await _pay(pid)
    assert await fulfillment.on_order_paid(paid) == "FAILED"
    stars_sender.fail = None
    assert await fulfillment.fulfill_order(o["public_id"]) == "DELIVERED"
    assert await fulfillment.fulfill_order(o["public_id"]) == "SKIPPED"
    assert len(stars_sender.sent) == 1


async def test_unknown_result_is_flagged_and_never_resent(tg, stars_sender, sent):
    pid = await _prep(tg, 100)
    stars_sender.fail = TimeoutError("lost connection")        # may or may not have been sent
    o, paid = await _pay(pid)
    assert await fulfillment.on_order_paid(paid) == "PENDING"
    cur = await orders.get_order(o["public_id"])
    assert cur["status"] == S.PROCESSING and cur["fulfillment_status"] == "MANUAL_PENDING"
    assert "نامشخص" in cur["manual_reason"] and any("نامشخص" in a for a in sent.admins)
    stars_sender.fail = None
    # neither a retry nor a direct call may send again
    assert await fulfillment.fulfill_order(o["public_id"]) == "SKIPPED"
    sup = await stars_delivery.StarsSupplier().place_order(
        __import__("supplier_base").SupplierRequest(o["public_id"], {"kind": "stars", "amount": 100}, 1, 1001))
    assert sup.status == "PENDING" and stars_sender.sent == []
    assert await fulfillment.complete_manual(o["public_id"], "admin1", "checked")
    assert (await orders.get_order(o["public_id"]))["status"] == S.FULFILLED


async def test_duplicate_payment_update_does_not_send_twice(tg, stars_sender):
    pid = await _prep(tg, 100)
    o, paid = await _pay(pid)
    await fulfillment.on_order_paid(paid)
    again = await stars.handle_successful_payment(1001, o["public_id"], "XTR", o["stars_amount"], "c1")
    assert again.code == "DUPLICATE"
    assert await fulfillment.fulfill_order(o["public_id"]) == "SKIPPED"
    assert len(stars_sender.sent) == 1


async def test_sender_unavailable_falls_back_to_manual(tg, stars_sender):
    pid = await _prep(tg, 100)
    stars_sender.available = False
    o, paid = await _pay(pid)
    assert await fulfillment.on_order_paid(paid) == "PENDING" and stars_sender.sent == []


# ------------------------------------------------------------------ order record + payment policy
async def test_order_records_amount_currency_and_method(tg):
    o = await orders.create_order(5, tg.stars, 1, "stars")
    assert o["currency"] == "XTR" and o["amount"] == o["stars_amount"] and o["payment_method"] == "stars"
    assert o["total_toman"] and o["price_snapshot"]["final_toman"] == o["unit_price_toman"] and o["created_at"]


async def test_external_providers_never_offered_for_digital_goods(tg):
    import payment_manager as pm
    from payment_base import PaymentProvider

    class FakeGateway(PaymentProvider):
        key, display_name, external = "fake_gateway", "x", True
        async def is_enabled(self): return True
        async def create_payment(self, order): raise NotImplementedError

    pm.register_provider(FakeGateway())
    try:
        digital = await catalog.get_product(tg.stars)
        assert "fake_gateway" not in [p.key for p in await pm.available_methods(digital)]
        physical = {**digital, "digital_in_telegram": False}
        assert "fake_gateway" in [p.key for p in await pm.available_methods(physical)]
    finally:
        pm._PROVIDERS.pop("fake_gateway")


async def test_stars_payment_rejected_for_order_of_another_method(tg, sent):
    o = await orders.create_order(5, tg.stars, 1, "wallet")
    ok, _ = await stars.pre_checkout(5, o["public_id"], "XTR", o["stars_amount"])
    assert not ok
    res = await stars.handle_successful_payment(5, o["public_id"], "XTR", o["stars_amount"], "cw")
    assert res.code == "ORDER_CLOSED"
    assert (await get_db().payments.find_one({"_id": "stars:cw"}))["status"] == "NEEDS_REFUND"
    assert (await orders.get_order(o["public_id"]))["status"] == S.PENDING_PAYMENT


async def test_supplier_called_twice_never_sends_twice(tg, stars_sender):
    from supplier_base import SupplierRequest
    pid = await _prep(tg, 100)
    o, paid = await _pay(pid)
    req = SupplierRequest(o["public_id"], {"kind": "stars", "amount": 100}, 1, 1001)
    first = await stars_delivery.StarsSupplier().place_order(req)
    second = await stars_delivery.StarsSupplier().place_order(req)
    assert first.status == second.status == "DELIVERED" and len(stars_sender.sent) == 1
