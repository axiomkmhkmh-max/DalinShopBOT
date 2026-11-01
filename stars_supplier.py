"""Stars SUPPLIER adapter layer (no database, no order logic: only talks to a supplier API).

Everything the shop knows about a supplier goes through `StarsSupplierAdapter`, so another supplier can be added later
by writing one more adapter class and registering it in `build_adapter()`.

Implemented adapter: `StardAdapter` (Stard Market API v1). Its endpoints, headers, statuses and error codes come from the
documentation that ships with the sample shop (docs/API.md of stard-shop-bot) - nothing here is guessed. NOT yet tested
against the live service from this project: use a test key (sk_test_...) first, see START_HERE.md.

Outcome vocabulary (the ONLY thing the rest of the shop sees):
  NormalizedStatus  ACCEPTED | PROCESSING | DELIVERED | FAILED | UNKNOWN      (what the supplier says about an order)
  SupplierRejected  the supplier DEFINITELY refused: nothing was created               -> safe to refund the customer
  SupplierTransient the outcome is UNKNOWN (timeout, network, 5xx, rate limit...)       -> NEVER refund, re-check first
  SupplierAuthError key / permission / supplier-wallet problem (needs the shop owner)   -> manual review, no refund
The API key is read from the environment only and is never logged or stored.
"""
from __future__ import annotations

import asyncio
import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import Enum
from typing import Awaitable, Callable

log = logging.getLogger(__name__)


class NormalizedStatus(str, Enum):
    ACCEPTED = "ACCEPTED"        # supplier has the order, nothing happened yet
    PROCESSING = "PROCESSING"    # supplier is working on it
    DELIVERED = "DELIVERED"      # supplier confirms the Stars arrived
    FAILED = "FAILED"            # supplier says it was cancelled / failed / refunded (its money went back to its wallet)
    UNKNOWN = "UNKNOWN"          # a status word we do not know: never treated as failure


class SupplierError(Exception):
    def __init__(self, message: str = "", *, code: str = "", http_status: int = 0, request_id: str = ""):
        super().__init__(message or code)
        self.code, self.http_status, self.request_id = code, http_status, request_id


class SupplierRejected(SupplierError):
    """Definite refusal (nothing created)."""


class SupplierTransient(SupplierError):
    """Unknown outcome."""


class SupplierAuthError(SupplierError):
    """Invalid key / missing scope / supplier wallet empty."""


@dataclass(frozen=True)
class SupplierQuote:
    amount_toman: int            # what the supplier will charge us
    quote_id: str = ""
    unit_price: int = 0


@dataclass(frozen=True)
class SupplierOrderInfo:
    ref: str                     # supplier's order id
    status: NormalizedStatus
    raw_status: str = ""
    failure_reason: str = ""
    amount_toman: int = 0
    replayed: bool = False       # the supplier answered an idempotent replay (no second order was created)


class StarsSupplierAdapter(ABC):
    name = "abstract"
    min_stars = 1
    max_stars = 10**9

    @abstractmethod
    async def ping(self) -> dict: ...                       # {"ok": bool, "environment": "test"|"live"|""}

    @abstractmethod
    async def balance(self) -> int | None: ...              # our wallet at the supplier (Toman) or None if unknown

    @abstractmethod
    async def quote(self, quantity: int) -> SupplierQuote: ...

    @abstractmethod
    async def create_order(self, *, idempotency_key: str, quantity: int, recipient: str,
                           metadata: dict | None = None) -> SupplierOrderInfo: ...

    @abstractmethod
    async def get_order(self, ref: str) -> SupplierOrderInfo: ...


# ----------------------------------------------------------------------------------------------- Stard Market API v1
Transport = Callable[..., Awaitable[tuple[int, object, dict]]]   # (method, url, headers, json, params, timeout)

_STATUS_MAP = {
    "pending": NormalizedStatus.ACCEPTED,
    "processing": NormalizedStatus.PROCESSING,
    "completed": NormalizedStatus.DELIVERED,
    "cancelled": NormalizedStatus.FAILED, "refunded": NormalizedStatus.FAILED, "failed": NormalizedStatus.FAILED,
}
# HTTP 4xx that mean "refused, nothing created". 401/402/403 are NOT here on purpose (they need the owner, see below).
_DEFINITE_CODES_BY_STATUS = {400, 404, 422}
_DEFINITE_409 = {"price_changed", "product_unavailable"}
_AUTH_STATUSES = {401, 402, 403}


async def _httpx_request(method: str, url: str, headers: dict, json, params, timeout: float):
    import httpx                                  # lazy: only real calls need it
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.request(method, url, headers=headers, json=json, params=params)
    except httpx.HTTPError as exc:
        raise SupplierTransient(f"network error: {type(exc).__name__}", code="network_error") from None
    try:
        body = resp.json() if resp.content else None
    except ValueError:
        body = None
    return resp.status_code, body, dict(resp.headers)


class StardAdapter(StarsSupplierAdapter):
    name = "stard"
    min_stars = 50                 # documented minimum for Stars (parameter_invalid below it)
    max_stars = 1_000_000

    def __init__(self, api_key: str, base_url: str = "https://stard-market.ir/api/v1", *, timeout: float = 20.0,
                 http_retries: int = 2, transport: Transport | None = None, sleep=asyncio.sleep):
        if not api_key:
            raise SupplierAuthError("supplier API key is not configured", code="no_api_key")
        self._key = api_key
        self._base = base_url.rstrip("/")
        self._timeout = timeout
        self._retries = max(0, int(http_retries))
        self._send = transport or _httpx_request
        self._sleep = sleep

    @property
    def environment(self) -> str:                    # from the key prefix only; the key itself is never exposed
        return "test" if self._key.startswith("sk_test_") else ("live" if self._key.startswith("sk_live_") else "")

    async def _request(self, method: str, path: str, *, json=None, params=None, idempotency_key: str | None = None):
        headers = {"Authorization": f"Bearer {self._key}", "User-Agent": "dalin-shop/1.0"}
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key
        can_retry = method == "GET" or idempotency_key is not None      # a POST is only repeated when it is replay-safe
        delay = 1.0
        for attempt in range(self._retries + 1):
            last = attempt == self._retries
            try:
                status, body, resp_headers = await self._send(method, f"{self._base}{path}", headers, json, params, self._timeout)
            except SupplierTransient:
                if not can_retry or last:
                    raise
                await self._sleep(delay); delay *= 2
                continue
            if status < 400:
                if not isinstance(body, (dict, list)):
                    raise SupplierTransient("unreadable response", code="bad_response", http_status=status)
                return body, resp_headers
            err = self._classify(status, body)
            transient = isinstance(err, SupplierTransient)
            if transient and can_retry and not last:
                wait = delay
                if status == 429:
                    try:
                        wait = min(30.0, float(resp_headers.get("Retry-After", delay)))
                    except (TypeError, ValueError):
                        pass
                await self._sleep(wait); delay *= 2
                continue
            raise err
        raise AssertionError("unreachable")

    @staticmethod
    def _classify(status: int, body) -> SupplierError:
        e = body.get("error") if isinstance(body, dict) and isinstance(body.get("error"), dict) else {}
        code, msg, rid = str(e.get("code") or ""), str(e.get("message") or "")[:200], str(e.get("request_id") or "")
        kw = dict(code=code or f"http_{status}", http_status=status, request_id=rid)
        if status in _AUTH_STATUSES:
            return SupplierAuthError(msg or f"http {status}", **kw)
        if status in _DEFINITE_CODES_BY_STATUS or (status == 409 and code in _DEFINITE_409):
            return SupplierRejected(msg or f"http {status}", **kw)
        # 429, 5xx, 409 request_in_progress and anything unexpected: the outcome of a POST is NOT known
        return SupplierTransient(msg or f"http {status}", **kw)

    @staticmethod
    def _order_info(body: dict, replayed: bool = False) -> SupplierOrderInfo:
        if not isinstance(body, dict) or not body.get("id"):
            raise SupplierTransient("order response without id", code="bad_response")
        raw = str(body.get("status") or "")
        amount = body.get("amount")
        amount = amount.get("amount") if isinstance(amount, dict) else None
        return SupplierOrderInfo(
            ref=str(body["id"]), status=_STATUS_MAP.get(raw.lower(), NormalizedStatus.UNKNOWN), raw_status=raw[:40],
            failure_reason=str(body.get("failure_reason") or "")[:200],
            amount_toman=int(amount) if isinstance(amount, (int, str)) and str(amount).isdigit() else 0, replayed=replayed)

    async def ping(self) -> dict:
        body, _ = await self._request("GET", "/ping")
        return {"ok": bool(body.get("ok")), "environment": str(body.get("environment") or self.environment)}

    async def balance(self) -> int | None:
        body, _ = await self._request("GET", "/wallet")
        for b in body.get("balances") or []:
            if isinstance(b, dict) and b.get("currency") == "IRT" and str(b.get("amount", "")).lstrip("-").isdigit():
                return int(b["amount"])
        return None

    async def quote(self, quantity: int) -> SupplierQuote:
        if not self.min_stars <= quantity <= self.max_stars:
            raise SupplierRejected(f"Stars amount must be {self.min_stars}..{self.max_stars}", code="parameter_invalid")
        body, _ = await self._request("POST", "/orders/quote", json={"type": "stars", "quantity": int(quantity)})
        amount = body.get("amount")
        if not isinstance(amount, int) or isinstance(amount, bool) or amount <= 0:
            raise SupplierTransient("quote without amount", code="bad_response")
        return SupplierQuote(amount, str(body.get("id") or ""), int(body.get("unit_price") or 0))

    async def create_order(self, *, idempotency_key: str, quantity: int, recipient: str,
                           metadata: dict | None = None) -> SupplierOrderInfo:
        body_in = {"type": "stars", "quantity": int(quantity), "recipient": "@" + recipient.lstrip("@")}
        if metadata:
            body_in["metadata"] = metadata
        body, headers = await self._request("POST", "/orders", json=body_in, idempotency_key=idempotency_key)
        replayed = str(headers.get("Idempotent-Replayed", headers.get("idempotent-replayed", ""))).lower() == "true"
        return self._order_info(body, replayed)

    async def get_order(self, ref: str) -> SupplierOrderInfo:
        if not ref or not all(c.isalnum() or c in "_-" for c in ref):
            raise SupplierRejected("invalid order reference", code="invalid_ref")
        body, _ = await self._request("GET", f"/orders/{ref}")
        return self._order_info(body)


# ----------------------------------------------------------------------------------------------- registry
_override: StarsSupplierAdapter | None = None


def register_adapter(adapter: StarsSupplierAdapter | None) -> None:       # tests
    global _override
    _override = adapter


def build_adapter() -> StarsSupplierAdapter:
    from config import get_config
    cfg = get_config()
    if cfg.stars_supplier_provider.lower() != "stard":
        raise SupplierAuthError(f"unknown supplier provider {cfg.stars_supplier_provider!r}", code="unknown_provider")
    return StardAdapter(cfg.stars_supplier_api_key, cfg.stars_supplier_api_base, timeout=cfg.stars_supplier_timeout)


def get_adapter() -> StarsSupplierAdapter:
    return _override or build_adapter()


def is_enabled() -> bool:
    if _override is not None:
        return True
    from config import get_config
    cfg = get_config()
    return bool(cfg.stars_supplier_enabled and cfg.stars_supplier_api_key)
