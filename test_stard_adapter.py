"""MOCK tests of the Stard adapter (fake transport built from the response samples in docs/API.md of the sample shop).
They prove OUR handling of statuses / errors / retries. They do NOT prove the live service behaves the same:
see test_stard_live.py (real API, opt-in)."""
import pytest

from stars_supplier import (NormalizedStatus as N, StardAdapter, SupplierAuthError, SupplierRejected, SupplierTransient)


def err(status, code, msg="x", headers=None):
    return status, {"error": {"type": "t", "code": code, "message": msg, "param": None, "request_id": "req_1"}}, headers or {}


def ok(body, status=200, headers=None):
    return status, body, headers or {}


class Fake:
    def __init__(self, *responses):
        self.responses, self.calls = list(responses), []

    async def __call__(self, method, url, headers, json, params, timeout):
        self.calls.append((method, url, dict(headers), json))
        r = self.responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


async def nosleep(_):
    return None


def adapter(*responses, retries=2, key="sk_test_SECRETKEY123"):
    t = Fake(*responses)
    return StardAdapter(key, "https://s.test/api/v1", http_retries=retries, transport=t, sleep=nosleep), t


ORDER = {"object": "order", "id": "ord_test_1", "status": "pending", "amount": {"amount": 2249000, "currency": "IRT"}, "failure_reason": None}


def test_requires_key():
    with pytest.raises(SupplierAuthError):
        StardAdapter("")


async def test_create_order_request_shape_and_idempotency_header():
    a, t = adapter(ok(ORDER, 201))
    info = await a.create_order(idempotency_key="shop-1", quantity=500, recipient="@bob_99", metadata={"shop_order_id": "x"})
    method, url, headers, body = t.calls[0]
    assert (method, url) == ("POST", "https://s.test/api/v1/orders")
    assert headers["Idempotency-Key"] == "shop-1" and headers["Authorization"] == "Bearer sk_test_SECRETKEY123"
    assert body == {"type": "stars", "quantity": 500, "recipient": "@bob_99", "metadata": {"shop_order_id": "x"}}
    assert info.ref == "ord_test_1" and info.status is N.ACCEPTED and info.amount_toman == 2249000 and not info.replayed


async def test_replay_header_is_reported():
    a, _ = adapter(ok(ORDER, 201, {"Idempotent-Replayed": "true"}))
    assert (await a.create_order(idempotency_key="k", quantity=50, recipient="bob_99")).replayed


@pytest.mark.parametrize("raw,expected", [("pending", N.ACCEPTED), ("processing", N.PROCESSING), ("completed", N.DELIVERED),
                                          ("cancelled", N.FAILED), ("refunded", N.FAILED), ("failed", N.FAILED),
                                          ("something_new", N.UNKNOWN), ("", N.UNKNOWN)])
async def test_status_mapping_unknown_is_never_failure(raw, expected):
    a, _ = adapter(ok({**ORDER, "status": raw}))
    assert (await a.get_order("ord_test_1")).status is expected


@pytest.mark.parametrize("status,code", [(400, "recipient_invalid"), (400, "parameter_invalid"), (404, "product_not_found"),
                                         (409, "price_changed"), (409, "product_unavailable"), (422, "x")])
async def test_definite_rejections(status, code):
    a, t = adapter(err(status, code))
    with pytest.raises(SupplierRejected) as e:
        await a.create_order(idempotency_key="k", quantity=50, recipient="bob_99")
    assert e.value.code == code and len(t.calls) == 1               # never retried


@pytest.mark.parametrize("status,code", [(401, "invalid_api_key"), (402, "insufficient_funds"), (403, "insufficient_scope")])
async def test_key_and_supplier_wallet_problems_are_not_refund_reasons(status, code):
    a, _ = adapter(err(status, code))
    with pytest.raises(SupplierAuthError):
        await a.create_order(idempotency_key="k", quantity=50, recipient="bob_99")


async def test_5xx_and_in_progress_are_unknown_not_rejected():
    for resp in (err(500, "oops"), err(409, "request_in_progress"), err(418, "teapot")):
        a, _ = adapter(resp, resp, resp)
        with pytest.raises(SupplierTransient):
            await a.create_order(idempotency_key="k", quantity=50, recipient="bob_99")


async def test_post_with_key_is_retried_then_succeeds_without_second_order():
    a, t = adapter(err(503, "down"), SupplierTransient("timeout", code="network_error"), ok(ORDER, 201))
    info = await a.create_order(idempotency_key="shop-9", quantity=50, recipient="bob_99")
    assert info.ref == "ord_test_1" and len(t.calls) == 3
    assert {c[2]["Idempotency-Key"] for c in t.calls} == {"shop-9"}   # every retry carries the SAME key


async def test_post_without_key_is_never_retried():
    a, t = adapter(err(503, "down"), ok({"id": "x", "amount": 5}))
    with pytest.raises(SupplierTransient):
        await a.quote(100)
    assert len(t.calls) == 1


async def test_get_is_retried_and_429_waits_retry_after():
    waits = []

    async def rec(s):
        waits.append(s)

    t = Fake(err(429, "rate_limit_exceeded", headers={"Retry-After": "3"}), ok({**ORDER, "status": "completed"}))
    a = StardAdapter("sk_test_k", "https://s.test/api/v1", transport=t, sleep=rec)
    assert (await a.get_order("ord_test_1")).status is N.DELIVERED and waits == [3.0]


async def test_timeout_after_all_retries_is_transient():
    a, t = adapter(*[SupplierTransient("t", code="network_error")] * 3)
    with pytest.raises(SupplierTransient):
        await a.get_order("ord_test_1")
    assert len(t.calls) == 3


async def test_garbage_responses_are_transient():
    for resp in ((200, "html", {}), (200, {"nope": 1}, {})):
        a, _ = adapter(resp, resp, resp)
        with pytest.raises(SupplierTransient):
            await a.get_order("ord_test_1")


async def test_quote_balance_ping():
    a, t = adapter(ok({"object": "quote", "id": "qt_1", "amount": 2249000, "unit_price": 4498}),
                   ok({"balances": [{"currency": "IRT", "amount": 5000}]}), ok({"ok": True, "environment": "test"}))
    q = await a.quote(500)
    assert (q.amount_toman, q.quote_id, q.unit_price) == (2249000, "qt_1", 4498)
    assert t.calls[0][3] == {"type": "stars", "quantity": 500}
    assert await a.balance() == 5000
    assert await a.ping() == {"ok": True, "environment": "test"}


async def test_quote_outside_supplier_limits_is_rejected_locally():
    a, t = adapter()
    for n in (0, 49, 1_000_001):
        with pytest.raises(SupplierRejected):
            await a.quote(n)
    assert t.calls == []


async def test_bad_order_reference_is_never_sent():
    a, t = adapter()
    with pytest.raises(SupplierRejected):
        await a.get_order("../wallet")
    assert t.calls == []


async def test_api_key_never_appears_in_error_text():
    a, _ = adapter(err(401, "invalid_api_key", "bad key sk_test_SECRETKEY123"))
    with pytest.raises(SupplierAuthError):
        await a.ping()
    # the message comes from the supplier; the shop redacts it before storing/logging (see supplier_tracking tests)
