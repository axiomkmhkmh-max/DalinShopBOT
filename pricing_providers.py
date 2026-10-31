"""Configurable Price / Exchange-rate providers.

Nothing here knows a real vendor API. `HttpJson*` providers call a URL YOU configure
(env vars) and read a numeric field using a JSON path. API keys come from ENV only.
Mock providers exist for tests/development and are refused in production.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

import httpx

from config import Config
from money import MoneyError, rial_to_toman, to_decimal
from util import utcnow


class ProviderError(Exception):
    pass


@dataclass(frozen=True)
class BasePrice:
    amount: Decimal
    currency: str        # USD / TON / IRT / IRR ...
    fetched_at: datetime


@dataclass(frozen=True)
class Rate:
    toman_per_unit: Decimal   # ALWAYS Toman per 1 unit of the currency (Rial already converted)
    fetched_at: datetime
    source: str


def extract_path(data, path: str):
    """'data.items.0.price' -> data['data']['items'][0]['price']"""
    cur = data
    for part in [p for p in path.split(".") if p != ""]:
        try:
            if isinstance(cur, list):
                cur = cur[int(part)]
            else:
                cur = cur[part]
        except (KeyError, IndexError, ValueError, TypeError) as exc:
            raise ProviderError(f"json path not found: {path}") from exc
    return cur


async def _http_get_json(url: str, header: str, prefix: str, key: str, timeout: float = 10.0):
    headers = {}
    if key:
        headers[header] = f"{prefix} {key}".strip()
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.get(url, headers=headers)
            resp.raise_for_status()
            return resp.json(parse_float=Decimal)   # parse_float=Decimal => never a float
    except (httpx.HTTPError, ValueError) as exc:
        raise ProviderError(f"request failed: {type(exc).__name__}") from exc


class PriceProvider(ABC):
    name = "abstract"

    @abstractmethod
    async def get_base_price(self, ref: str) -> BasePrice: ...


class RateProvider(ABC):
    name = "abstract"

    @abstractmethod
    async def get_rate(self, currency: str) -> Rate: ...


class HttpJsonPriceProvider(PriceProvider):
    name = "http_json"

    def __init__(self, cfg: Config):
        self.cfg = cfg

    async def get_base_price(self, ref: str) -> BasePrice:
        if not (self.cfg.price_api_url and self.cfg.price_json_path):
            raise ProviderError("price provider not configured (PRICE_API_URL / PRICE_JSON_PATH)")
        url = self.cfg.price_api_url.replace("{ref}", ref)
        data = await _http_get_json(
            url, self.cfg.price_api_key_header, self.cfg.price_api_key_prefix, self.cfg.price_api_key
        )
        try:
            amount = to_decimal(extract_path(data, self.cfg.price_json_path))
        except MoneyError as exc:
            raise ProviderError("invalid price value") from exc
        if amount <= 0:
            raise ProviderError("price must be positive")
        return BasePrice(amount, self.cfg.price_currency.upper(), utcnow())


class HttpJsonRateProvider(RateProvider):
    name = "http_json"

    def __init__(self, cfg: Config):
        self.cfg = cfg

    async def get_rate(self, currency: str) -> Rate:
        if not (self.cfg.rate_api_url and self.cfg.rate_json_path):
            raise ProviderError("rate provider not configured (RATE_API_URL / RATE_JSON_PATH)")
        url = self.cfg.rate_api_url.replace("{currency}", currency.upper())
        data = await _http_get_json(
            url, self.cfg.rate_api_key_header, self.cfg.rate_api_key_prefix, self.cfg.rate_api_key
        )
        try:
            value = to_decimal(extract_path(data, self.cfg.rate_json_path))
        except MoneyError as exc:
            raise ProviderError("invalid rate value") from exc
        if value <= 0:
            raise ProviderError("rate must be positive")
        unit = self.cfg.rate_unit.upper()
        if unit == "IRR":
            value = rial_to_toman(value)          # explicit Rial -> Toman
        elif unit != "IRT":
            raise ProviderError("RATE_UNIT must be IRT or IRR")
        return Rate(value, utcnow(), self.name)


class MockPriceProvider(PriceProvider):
    """TEST/DEV ONLY. Values are injected by the test, never real."""
    name = "mock"

    def __init__(self, prices: dict[str, tuple[str, str]] | None = None, fail: bool = False):
        self.prices = prices or {}
        self.fail = fail

    async def get_base_price(self, ref: str) -> BasePrice:
        if self.fail or ref not in self.prices:
            raise ProviderError("mock price failure")
        amount, cur = self.prices[ref]
        return BasePrice(to_decimal(amount), cur, utcnow())


class MockRateProvider(RateProvider):
    name = "mock"

    def __init__(self, rates: dict[str, str] | None = None, fail: bool = False):
        self.rates = rates or {}
        self.fail = fail

    async def get_rate(self, currency: str) -> Rate:
        if self.fail or currency not in self.rates:
            raise ProviderError("mock rate failure")
        return Rate(to_decimal(self.rates[currency]), utcnow(), self.name)


def build_price_provider(name: str, cfg: Config) -> PriceProvider:
    if name == "http_json":
        return HttpJsonPriceProvider(cfg)
    if name == "mock" and not cfg.is_production:
        return MockPriceProvider()
    raise ProviderError(f"unknown or forbidden price provider: {name}")


def build_rate_provider(name: str, cfg: Config) -> RateProvider:
    if name == "http_json":
        return HttpJsonRateProvider(cfg)
    if name == "mock" and not cfg.is_production:
        return MockRateProvider()
    raise ProviderError(f"unknown or forbidden rate provider: {name}")
