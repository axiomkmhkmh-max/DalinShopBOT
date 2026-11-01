"""Zarinpal client against a FAKE transport (no network). Response shapes follow the official docs."""
import pytest

from zarinpal import ZarinpalClient, ZarinpalError, ZarinpalUnavailable

AUTH = "A00000000000000000000000000000wwOGYpd"


class FakeTransport:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    async def __call__(self, url, payload):
        self.calls.append((url, payload))
        r = self.responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


def ok(**data):
    return 200, {"data": {"code": 100, "message": "Success", **data}, "errors": []}


def err(code, msg="x", http=200):
    return http, {"data": [], "errors": {"code": code, "message": msg}}


def test_requires_merchant_id():
    with pytest.raises(ZarinpalError):
        ZarinpalClient("")


async def test_request_payment_success_and_urls():
    t = FakeTransport(ok(authority=AUTH))
    c = ZarinpalClient("m-id", transport=t)
    assert await c.request_payment(100000, "https://x/cb?t=1", "شارژ") == AUTH
    url, payload = t.calls[0]
    assert url == "https://payment.zarinpal.com/pg/v4/payment/request.json"
    assert payload["amount"] == 100000 and payload["merchant_id"] == "m-id"
    assert c.start_pay_url(AUTH) == f"https://payment.zarinpal.com/pg/StartPay/{AUTH}"
    assert ZarinpalClient("m", sandbox=True).start_pay_url(AUTH).startswith("https://sandbox.zarinpal.com/")


async def test_request_payment_refused_and_invalid_amount():
    c = ZarinpalClient("m", transport=FakeTransport(err(-9, "Validation error", http=400)))
    with pytest.raises(ZarinpalError) as e:
        await c.request_payment(1000, "https://x", "d")
    assert e.value.code == -9
    for bad in (0, -5, 1.5, True):
        with pytest.raises(ZarinpalError):
            await ZarinpalClient("m", transport=FakeTransport()).request_payment(bad, "https://x", "d")


async def test_verify_100_and_101_are_paid():
    t = FakeTransport(ok(ref_id=201, card_pan="502229******1234"), (200, {"data": {"code": 101, "message": "Verified"}, "errors": []}))
    c = ZarinpalClient("m", transport=t)
    first = await c.verify(100000, AUTH)
    again = await c.verify(100000, AUTH)
    assert first.paid and first.code == 100 and first.ref_id == 201 and first.card_pan == "502229******1234"
    assert again.paid and again.code == 101
    assert t.calls[0][1]["authority"] == AUTH and t.calls[0][1]["amount"] == 100000


@pytest.mark.parametrize("code", [-51, -53, -54, -50, 0, 200])
async def test_verify_other_codes_are_not_paid(code):
    res = await ZarinpalClient("m", transport=FakeTransport(err(code))).verify(1000, AUTH)
    assert not res.paid and res.code == code


async def test_unknown_outcomes_raise_unavailable():
    for resp in ((502, None), (200, "garbage"), (200, {"nope": 1}), ZarinpalUnavailable("timeout")):
        with pytest.raises(ZarinpalUnavailable):
            await ZarinpalClient("m", transport=FakeTransport(resp)).verify(1000, AUTH)


async def test_bad_authority_rejected_before_any_call():
    t = FakeTransport()
    with pytest.raises(ZarinpalError):
        await ZarinpalClient("m", transport=t).verify(1000, "../evil")
    assert t.calls == []
