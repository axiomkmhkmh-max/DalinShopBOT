"""Zarinpal wallet top-up with a FAKE gateway (no network, no real money). Covers: credit exactly once, duplicate /
concurrent callbacks, cancelled / unpaid / forged callbacks, unknown outcome + reconcile, amount tampering."""
import asyncio
from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest

import topups, wallet
import settings as st
from db import get_db
from util import utcnow
from zarinpal import VerifyResult, ZarinpalError, ZarinpalUnavailable


class FakeGateway:
    """Stands in for ZarinpalClient. `paid[authority] = amount_rial` simulates what the customer really paid."""

    def __init__(self):
        self.n = 0
        self.requests, self.verify_calls, self.paid, self.verified = [], [], {}, set()
        self.down = False
        self.refuse = False

    async def request_payment(self, amount_rial, callback_url, description):
        if self.refuse:
            raise ZarinpalError(-9, "validation")
        if self.down:
            raise ZarinpalUnavailable("down")
        self.n += 1
        authority = f"A{self.n:035d}"
        self.requests.append((authority, amount_rial, callback_url))
        return authority

    def start_pay_url(self, authority):
        return f"https://pay.test/StartPay/{authority}"

    async def verify(self, amount_rial, authority):
        self.verify_calls.append((amount_rial, authority))
        if self.down:
            raise ZarinpalUnavailable("down")
        if self.paid.get(authority) == amount_rial:
            code = 101 if authority in self.verified else 100      # like the real API: 100 once, then 101
            self.verified.add(authority)
            return VerifyResult(code, True, 555, "502229******1234")
        return VerifyResult(-51, False)


@pytest.fixture
async def zp(db, sent, monkeypatch):
    await st.set_setting("zarinpal_topup_enabled", "true")
    monkeypatch.setattr(topups, "get_config", lambda: SimpleNamespace(zarinpal_merchant_id="m", base_url="https://shop.test"))
    return FakeGateway()


async def _new(zp, user=7, amount=100_000):
    row = await topups.create_topup(user, amount, client=zp)
    return row, zp.requests[-1][0]


async def test_create_topup_asks_gateway_in_rial_with_callback_id(zp):
    row, authority = await _new(zp)
    assert row["status"] == topups.PENDING and row["pay_url"].endswith(authority)
    _, rial, callback = zp.requests[0]
    assert rial == 1_000_000 and callback == f"https://shop.test/pay/zarinpal/callback?t={row['public_id']}"
    assert await wallet.get_balance(7) == 0                      # creating a top-up never touches the balance


@pytest.mark.parametrize("amount", [9_999, 5_000_001, 0, -5, True, 1.5])
async def test_amount_limits_and_types(zp, amount):
    with pytest.raises(topups.TopupError):
        await topups.create_topup(7, amount, client=zp)
    assert await get_db().topups.count_documents({}) == 0 and zp.requests == []


async def test_disabled_feature_creates_nothing(zp):
    await st.set_setting("zarinpal_topup_enabled", "false")
    with pytest.raises(topups.TopupError):
        await topups.create_topup(7, 100_000, client=zp)
    assert zp.requests == []


async def test_gateway_refusal_and_outage_leave_no_pending_row(zp):
    zp.refuse = True
    with pytest.raises(topups.TopupError):
        await topups.create_topup(7, 100_000, client=zp)
    zp.refuse, zp.down = False, True
    with pytest.raises(topups.TopupError):
        await topups.create_topup(7, 100_000, client=zp)
    rows = [t async for t in get_db().topups.find({})]
    assert len(rows) == 2 and all(t["status"] == topups.FAILED for t in rows)


async def test_verified_payment_credits_exactly_the_stored_amount_once(zp, sent):
    row, authority = await _new(zp)
    zp.paid[authority] = 1_000_000
    res = await topups.settle(row["public_id"], authority, "OK", client=zp)
    assert res.code == "PAID" and await wallet.get_balance(7) == 100_000
    assert (await wallet.reconcile(7))["ok"]
    t = await topups.get_topup(row["public_id"])
    assert t["status"] == topups.PAID and t["ref_id"] == 555
    assert len(sent.users) == 1 and "شارژ شد" in sent.users[0][1]


async def test_replayed_callback_does_not_credit_twice(zp):
    row, authority = await _new(zp)
    zp.paid[authority] = 1_000_000
    first = await topups.settle(row["public_id"], authority, "OK", client=zp)
    second = await topups.settle(row["public_id"], authority, "OK", client=zp)
    assert (first.code, second.code) == ("PAID", "ALREADY_PAID")
    assert await wallet.get_balance(7) == 100_000
    assert await get_db().wallet_ledger.count_documents({"type": "TOPUP"}) == 1
    assert len(zp.verify_calls) == 1                              # the second callback did not even need the gateway


async def test_concurrent_callbacks_credit_once(zp, sent):
    row, authority = await _new(zp)
    zp.paid[authority] = 1_000_000
    results = await asyncio.gather(*[topups.settle(row["public_id"], authority, "OK", client=zp) for _ in range(4)])
    assert await wallet.get_balance(7) == 100_000
    assert await get_db().wallet_ledger.count_documents({"type": "TOPUP"}) == 1
    assert sum(1 for r in results if r.code == "PAID") == 1
    assert len(sent.users) == 1


async def test_crash_after_credit_before_status_flip_is_healed_without_double_credit(zp):
    row, authority = await _new(zp)
    zp.paid[authority] = 1_000_000
    await wallet.credit(7, 100_000, idem_key=f"topup:{row['public_id']}", kind="TOPUP")   # the crashed attempt
    res = await topups.settle(row["public_id"], authority, "OK", client=zp)
    assert res.code == "PAID" and await wallet.get_balance(7) == 100_000


async def test_cancelled_callback_never_credits_and_never_asks_gateway(zp):
    row, authority = await _new(zp)
    res = await topups.settle(row["public_id"], authority, "NOK", client=zp)
    assert res.code == "CANCELLED" and await wallet.get_balance(7) == 0 and zp.verify_calls == []
    assert (await topups.get_topup(row["public_id"]))["status"] == topups.CANCELLED


async def test_status_ok_without_real_payment_is_not_credited(zp):
    row, authority = await _new(zp)                               # customer came back with Status=OK but never paid
    res = await topups.settle(row["public_id"], authority, "OK", client=zp)
    assert res.code == "NOT_PAID" and await wallet.get_balance(7) == 0
    assert (await topups.get_topup(row["public_id"]))["status"] == topups.PENDING


async def test_underpayment_at_gateway_is_not_credited(zp):
    row, authority = await _new(zp)
    zp.paid[authority] = 500_000                                  # gateway holds a different amount than we stored
    res = await topups.settle(row["public_id"], authority, "OK", client=zp)
    assert res.code == "NOT_PAID" and await wallet.get_balance(7) == 0


async def test_forged_callbacks_change_nothing(zp):
    row, authority = await _new(zp)
    zp.paid[authority] = 1_000_000
    assert (await topups.settle(row["public_id"], "A" + "9" * 35, "OK", client=zp)).code == "UNKNOWN"
    assert (await topups.settle("0" * 32, authority, "OK", client=zp)).code == "UNKNOWN"
    assert (await topups.settle(row["public_id"], "", "OK", client=zp)).code == "UNKNOWN"
    assert await wallet.get_balance(7) == 0 and (await topups.get_topup(row["public_id"]))["status"] == topups.PENDING


async def test_other_users_topup_cannot_credit_me(zp):
    a, auth_a = await _new(zp, user=1)
    b, auth_b = await _new(zp, user=2)
    zp.paid[auth_a] = 1_000_000
    await topups.settle(a["public_id"], auth_b, "OK", client=zp)        # a's id with b's authority
    assert await wallet.get_balance(1) == 0 and await wallet.get_balance(2) == 0


async def test_gateway_down_keeps_pending_and_reconcile_credits_once(zp):
    row, authority = await _new(zp)
    zp.paid[authority] = 1_000_000
    zp.down = True
    res = await topups.settle(row["public_id"], authority, "OK", client=zp)
    assert res.code == "UNAVAILABLE" and await wallet.get_balance(7) == 0
    assert (await topups.get_topup(row["public_id"]))["status"] == topups.PENDING       # unknown, NOT failed
    zp.down = False
    await get_db().topups.update_one({"public_id": row["public_id"]}, {"$set": {"created_at": utcnow() - timedelta(minutes=30)}})
    out = await topups.reconcile_pending(client=zp)
    again = await topups.reconcile_pending(client=zp)
    assert out["paid"] == 1 and again["paid"] == 0 and await wallet.get_balance(7) == 100_000


async def test_reconcile_expires_old_unpaid_rows_and_ignores_cancelled(zp):
    old, _ = await _new(zp)
    cancelled, auth_c = await _new(zp)
    await topups.settle(cancelled["public_id"], auth_c, "NOK", client=zp)
    await get_db().topups.update_many({}, {"$set": {"created_at": utcnow() - timedelta(hours=25)}})
    out = await topups.reconcile_pending(client=zp)
    assert out["expired"] == 1
    assert (await topups.get_topup(old["public_id"]))["status"] == topups.EXPIRED
    assert (await topups.get_topup(cancelled["public_id"]))["status"] == topups.CANCELLED


async def test_too_many_open_topups_are_refused(zp):
    for _ in range(topups.MAX_OPEN_PER_USER):
        await _new(zp)
    with pytest.raises(topups.TopupError):
        await topups.create_topup(7, 100_000, client=zp)


def test_suggest_amount_covers_missing_and_respects_minimum():
    assert topups.suggest_amount(12_345, 10_000) == 13_000
    assert topups.suggest_amount(500, 10_000) == 10_000
    assert topups.suggest_amount(20_000, 10_000) == 20_000
