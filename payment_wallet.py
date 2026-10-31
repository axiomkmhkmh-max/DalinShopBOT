"""Pay an order from the internal wallet (disabled by default via setting wallet_payment_enabled)."""
from __future__ import annotations

from constants import OrderStatus as S
from payment_base import PaymentInit, PaymentProvider, ProviderDisabled
import orders, wallet
import settings as st
from util import utcnow


class WalletProvider(PaymentProvider):
    key = "wallet"
    display_name = "پرداخت از کیف پول"

    async def is_enabled(self) -> bool:
        return await st.get_bool("wallet_payment_enabled")

    async def create_payment(self, order: dict) -> PaymentInit:
        return PaymentInit("internal", {"order_id": order["public_id"]})

    async def pay(self, order: dict) -> dict | None:
        """Debit (idempotent per order) then atomically mark PAID. Returns updated order or None."""
        if not await self.is_enabled():
            raise ProviderDisabled("wallet payment disabled")
        if order["status"] != S.PENDING_PAYMENT:
            return None
        try:
            await wallet.debit(order["user_id"], order["total_toman"], idem_key=f"order:{order['public_id']}",
                               kind="PURCHASE", ref=order["public_id"])
        except wallet.DuplicateTransaction:
            pass   # already debited for this order (retry) -> continue to mark PAID
        return await orders.transition(
            order["public_id"], S.PAID, from_states=[S.PENDING_PAYMENT],
            set_fields={"paid_at": utcnow(), "paid_charge_id": f"wallet:{order['public_id']}", "payment_status": "PAID"},
            note="wallet payment")
