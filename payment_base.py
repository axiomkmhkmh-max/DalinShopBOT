"""Common adapter interface for every payment provider.

HOW TO ADD A NEW PAYMENT METHOD LATER (e.g. a bank gateway): create `payment_<name>.py` with a PaymentProvider
subclass, set `external = True` if the money does not move through Telegram Stars, and register it in
payment_manager._PROVIDERS. Orders, price snapshots, stock, fulfillment and refunds need no change: a provider
only has to (1) create the payment for an order, (2) verify it on the SERVER, and (3) turn the order PENDING_PAYMENT
-> PAID through orders.transition (atomic) with a unique payment id, exactly like payment_stars.py.

TELEGRAM RULE (https://core.telegram.org/bots/payments-stars): digital goods and services sold inside a bot must be
paid exclusively with Telegram Stars; other currencies / third-party providers are not allowed (Telegram may hide
the bot from mobile users). payment_manager therefore never offers an `external` provider for digital products.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field


class ProviderDisabled(Exception):
    pass


@dataclass
class PaymentResult:
    code: str            # OK | DUPLICATE | MISMATCH | UNKNOWN_ORDER | ORDER_CLOSED
    order: dict | None = None


@dataclass
class PaymentInit:
    kind: str                       # telegram_invoice | redirect | ton_transfer | internal
    data: dict = field(default_factory=dict)


class PaymentProvider(ABC):
    key = "abstract"
    display_name = "abstract"
    external = False        # True = money does NOT move through Telegram Stars (never offered for digital goods)

    @abstractmethod
    async def is_enabled(self) -> bool: ...

    def supports_product(self, product: dict) -> bool:
        return True

    @abstractmethod
    async def create_payment(self, order: dict) -> PaymentInit: ...

    async def refund(self, order: dict, **kwargs) -> bool:
        raise NotImplementedError
