"""Zarinpal payment-gateway client (REST v4).

Official flow (https://www.zarinpal.com/docs/paymentGateway/connectToGateway):
  1. POST {api}/pg/v4/payment/request.json  {merchant_id, amount, callback_url, description}  -> data.code == 100 + data.authority
  2. redirect the customer to {pay}/pg/StartPay/{authority}
  3. Zarinpal redirects back to callback_url with ?Authority=...&Status=OK|NOK   (NEVER trust this by itself)
  4. POST {api}/pg/v4/payment/verify.json {merchant_id, amount, authority} -> code 100 (first time) / 101 (already verified)

Amounts here are in RIAL (the API's default currency). 1 Toman = 10 Rial; callers convert.
Anything other than code 100/101 from verify is "NOT paid". A network error / timeout is "UNKNOWN" (ZarinpalUnavailable):
callers must NOT treat it as failure, because the customer may really have paid.
No merchant id, authority secret or request body is ever logged.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Awaitable, Callable

log = logging.getLogger(__name__)

PROD_API = "https://payment.zarinpal.com"
SANDBOX_API = "https://sandbox.zarinpal.com"
_AUTHORITY_RE = re.compile(r"[A-Za-z0-9]{10,64}")

Transport = Callable[[str, dict], Awaitable[tuple[int, object]]]


class ZarinpalError(Exception):
    """The gateway (or our config) DEFINITELY refused: nothing was created / nothing is paid."""

    def __init__(self, code: int, message: str = ""):
        super().__init__(f"zarinpal error {code}: {message}")
        self.code = code
        self.message = message


class ZarinpalUnavailable(Exception):
    """Outcome UNKNOWN (network, timeout, 5xx, unreadable answer). Never mark a payment failed because of this."""


@dataclass(frozen=True)
class VerifyResult:
    code: int
    paid: bool                  # True only for 100 (verified now) or 101 (verified earlier)
    ref_id: int | None = None
    card_pan: str | None = None  # already masked by Zarinpal
    message: str = ""


def valid_authority(value: str | None) -> bool:
    return bool(value) and _AUTHORITY_RE.fullmatch(value) is not None


async def _httpx_post(url: str, payload: dict) -> tuple[int, object]:
    import httpx                      # lazy: only needed for real calls
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(url, json=payload, headers={"accept": "application/json"})
    except httpx.HTTPError as exc:
        raise ZarinpalUnavailable(type(exc).__name__) from None
    try:
        body = resp.json()
    except ValueError:
        body = None
    return resp.status_code, body


def _parse(body) -> tuple[int, dict]:
    """-> (code, data). Success: {"data": {"code": 100, ...}, "errors": []}. Error: {"data": [], "errors": {"code": -51, ...}}."""
    if not isinstance(body, dict):
        raise ZarinpalUnavailable("unreadable response")
    data = body.get("data")
    if isinstance(data, dict) and "code" in data:
        try:
            return int(data["code"]), data
        except (TypeError, ValueError):
            raise ZarinpalUnavailable("bad code") from None
    errors = body.get("errors")
    if isinstance(errors, list) and errors:
        errors = errors[0]
    if isinstance(errors, dict) and "code" in errors:
        try:
            return int(errors["code"]), {"message": str(errors.get("message", ""))[:200]}
        except (TypeError, ValueError):
            raise ZarinpalUnavailable("bad error code") from None
    raise ZarinpalUnavailable("unexpected response")


class ZarinpalClient:
    def __init__(self, merchant_id: str, *, sandbox: bool = False, transport: Transport | None = None):
        if not merchant_id:
            raise ZarinpalError(-1000, "merchant id is not configured")
        self._merchant = merchant_id
        self.sandbox = sandbox
        self._post = transport or _httpx_post

    @property
    def api_base(self) -> str:
        return SANDBOX_API if self.sandbox else PROD_API

    def start_pay_url(self, authority: str) -> str:
        if not valid_authority(authority):
            raise ZarinpalError(-1001, "invalid authority")
        return f"{self.api_base}/pg/StartPay/{authority}"

    async def _call(self, path: str, payload: dict) -> tuple[int, dict]:
        status, body = await self._post(f"{self.api_base}/pg/v4/payment/{path}.json", {"merchant_id": self._merchant, **payload})
        if status >= 500:
            raise ZarinpalUnavailable(f"http {status}")
        return _parse(body)

    async def request_payment(self, amount_rial: int, callback_url: str, description: str) -> str:
        """-> authority. Raises ZarinpalError (refused) / ZarinpalUnavailable (unknown)."""
        if isinstance(amount_rial, bool) or not isinstance(amount_rial, int) or amount_rial <= 0:
            raise ZarinpalError(-1002, "amount must be a positive integer (Rial)")
        code, data = await self._call("request", {
            "amount": amount_rial, "callback_url": callback_url, "description": description[:500]})
        authority = data.get("authority")
        if code != 100 or not valid_authority(authority):
            raise ZarinpalError(code, str(data.get("message", ""))[:200])
        return authority

    async def verify(self, amount_rial: int, authority: str) -> VerifyResult:
        """Server-side verification. paid == True only for code 100/101. Raises ZarinpalUnavailable when unknown."""
        if not valid_authority(authority):
            raise ZarinpalError(-1001, "invalid authority")
        code, data = await self._call("verify", {"amount": int(amount_rial), "authority": authority})
        if code in (100, 101):
            ref = data.get("ref_id")
            return VerifyResult(code, True, int(ref) if isinstance(ref, (int, str)) and str(ref).isdigit() else None,
                                data.get("card_pan"), str(data.get("message", ""))[:100])
        return VerifyResult(code, False, None, None, str(data.get("message", ""))[:200])


def get_client() -> ZarinpalClient:
    from config import get_config
    cfg = get_config()
    if cfg.zarinpal_sandbox and cfg.is_production:
        raise ZarinpalError(-1003, "sandbox mode is refused in production")
    return ZarinpalClient(cfg.zarinpal_merchant_id, sandbox=cfg.zarinpal_sandbox)
