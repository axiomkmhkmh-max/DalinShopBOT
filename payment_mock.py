"""Mock payment: TEST/DEV ONLY. Never simulates a real payment and is refused in production."""
from __future__ import annotations

from config import get_config
from payment_base import PaymentInit, PaymentProvider, ProviderDisabled


class MockPaymentProvider(PaymentProvider):
    key = "mock"
    display_name = "پرداخت آزمایشی (فقط توسعه)"

    async def is_enabled(self) -> bool:
        cfg = get_config()
        return cfg.enable_mock_payment and not cfg.is_production

    async def create_payment(self, order: dict) -> PaymentInit:
        if not await self.is_enabled():
            raise ProviderDisabled("mock payment disabled")
        return PaymentInit("internal", {"order_id": order["public_id"], "note": "mock only"})
