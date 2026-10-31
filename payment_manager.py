"""Central Payment Manager: decides which methods a customer may see.

The Telegram rule lives HERE, in one place: digital goods sold in the bot may only be paid with Telegram Stars,
so every `external` provider (TON, bank gateways, card-to-card ...) is hidden for digital products.
All current products (Stars, Premium, Gifts) are digital. See payment_base.py for how to add a provider.
"""
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


def register_provider(provider: PaymentProvider) -> None:
    _PROVIDERS[provider.key] = provider


async def available_methods(product: dict) -> list[PaymentProvider]:
    """Only enabled providers that are allowed for this product."""
    digital = product.get("digital_in_telegram", True)
    out = []
    for p in _PROVIDERS.values():
        if p.external and digital:
            continue                       # Telegram rule: digital goods => Stars only
        if await p.is_enabled() and p.supports_product(product):
            out.append(p)
    return out
