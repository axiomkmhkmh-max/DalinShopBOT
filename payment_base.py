"""Common adapter interface for every payment provider."""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field


class ProviderDisabled(Exception):
    pass


@dataclass
class PaymentInit:
    kind: str                       # telegram_invoice | redirect | ton_transfer | internal
    data: dict = field(default_factory=dict)


class PaymentProvider(ABC):
    key = "abstract"
    display_name = "abstract"

    @abstractmethod
    async def is_enabled(self) -> bool: ...

    def supports_product(self, product: dict) -> bool:
        return True

    @abstractmethod
    async def create_payment(self, order: dict) -> PaymentInit: ...

    async def refund(self, order: dict, **kwargs) -> bool:
        raise NotImplementedError
