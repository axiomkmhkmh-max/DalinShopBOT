"""Central Payment Manager: decides which methods a customer may see."""
from __future__ import annotations

from payment_base import PaymentProvider
from payment_mock import MockPaymentProvider
from payment_stars import StarsProvider
from payment_ton import TonProvider
from payment_wallet import WalletProvider

_PROVIDERS: dict[str, PaymentProvider] = {
    p.key: p for p in (StarsProvider(), WalletProvider(), TonProvider(), MockPaymentProvider())
}


def get_provider(key: str) -> PaymentProvider:
    return _PROVIDERS[key]


async def available_methods(product: dict) -> list[PaymentProvider]:
    """Only enabled providers that are allowed for this product.
    Digital products sold in Telegram (default) => TON is NEVER returned."""
    out = []
    for p in _PROVIDERS.values():
        if p.key in ("ton",) and product.get("digital_in_telegram", True):
            continue
        if await p.is_enabled() and p.supports_product(product):
            out.append(p)
    return out
