"""TON (Tonkeeper wallet-to-wallet) adapter — DISABLED.

Not active for digital products sold inside Telegram: Telegram requires Stars for those.
Only usable for a separate product/service that is permitted to use external payments.

What would be required before enabling:
  * written confirmation that the product/service is allowed to use external payments
  * a receiving TON wallet address (env var, never in code) and a unique comment/memo per order
  * server-side verification of the incoming transaction on-chain via a TON indexer API you choose
    (amount, destination, memo, confirmations, expiry)
  * legal/compliance review for your country
"""
from __future__ import annotations

from payment_base import PaymentInit, PaymentProvider, ProviderDisabled


class TonProvider(PaymentProvider):
    key = "ton"
    display_name = "پرداخت با TON (کیف‌به‌کیف)"

    async def is_enabled(self) -> bool:
        return False

    def supports_product(self, product: dict) -> bool:
        return not product.get("digital_in_telegram", True)

    async def create_payment(self, order: dict) -> PaymentInit:
        raise ProviderDisabled("TON adapter is disabled")
