"""Exchange-rate provider (USD -> Toman).

Nothing here knows a real vendor API. `HttpJsonRateProvider` calls a URL YOU configure (env vars) and reads a
numeric field using a JSON path. API keys come from ENV only and are never logged or shown.
If no URL is configured the provider is simply "not configured" and the rate is typed by hand in the dashboard.
Product base prices do not need a provider: Telegram Gifts/Premium prices come from the Telegram Bot API
(see telegram_supply.py) and Stars/other prices are entered in the dashboard.
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


class RateProvider(ABC):
    name = "abstract"
    configured = True

    @abstractmethod
    async def get_rate(self, currency: str) -> Rate: ...


class HttpJsonRateProvider(RateProvider):
    name = "http_json"

    def __init__(self, cfg: Config):
        self.cfg = cfg

    @property
    def configured(self) -> bool:  # type: ignore[override]
        return bool(self.cfg.rate_api_url and self.cfg.rate_json_path)

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


class MockRateProvider(RateProvider):
    name = "mock"

    def __init__(self, rates: dict[str, str] | None = None, fail: bool = False):
        self.rates = rates or {}
        self.fail = fail

    async def get_rate(self, currency: str) -> Rate:
        if self.fail or currency not in self.rates:
            raise ProviderError("mock rate failure")
        return Rate(to_decimal(self.rates[currency]), utcnow(), self.name)


def build_rate_provider(cfg: Config) -> RateProvider:
    return HttpJsonRateProvider(cfg)
