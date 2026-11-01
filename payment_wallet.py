"""Pay an order from the internal wallet (disabled by default via setting wallet_payment_enabled).

Safety rules
  * debit is idempotent per order (ledger idem_key "order:<id>"): a double tap / replay never charges twice
  * an expired or already-closed order is never charged
  * if the debit went through but the order could not become PAID (it was cancelled in between), the debit is reverted
  * refund is idempotent per order (ledger idem_key "refund:<id>") and needs a reason
"""
from __future__ import annotations

import logging

from constants import OrderStatus as S
from payment_base import PaymentInit, PaymentProvider, ProviderDisabled
import access, orders, wallet
import settings as st
from util import as_aware, utcnow

log = logging.getLogger(__name__)


class WalletProvider(PaymentProvider):
    key = "wallet"
    display_name = "پرداخت از کیف پول"

    async def is_enabled(self) -> bool:
        return await st.get_bool("wallet_payment_enabled")

    async def create_payment(self, order: dict) -> PaymentInit:
        return PaymentInit("internal", {"order_id": order["public_id"]})

    async def pay(self, order: dict) -> dict | None:
        """Debit (idempotent per order) then atomically mark PAID. Returns updated order or None.
        Raises wallet.InsufficientFunds when the balance is too low (nothing is charged)."""
        if not await self.is_enabled():
            raise ProviderDisabled("wallet payment disabled")
        allowed, why = access.check_payment_access(order["user_id"])
        if not allowed:
            raise ProviderDisabled(why)
        if order["status"] != S.PENDING_PAYMENT or as_aware(order["expires_at"]) <= utcnow():
            return None
        applied = True
        try:
            await wallet.debit(order["user_id"], order["total_toman"], idem_key=f"order:{order['public_id']}",
                               kind="PURCHASE", ref=order["public_id"])
        except wallet.DuplicateTransaction:
            applied = False   # already debited for this order (retry / double tap) -> continue to mark PAID
        updated = await orders.transition(
            order["public_id"], S.PAID, from_states=[S.PENDING_PAYMENT],
            set_fields={"paid_at": utcnow(), "paid_charge_id": f"wallet:{order['public_id']}", "payment_status": "PAID"},
            note="wallet payment")
        if updated is None and applied:
            fresh = await orders.get_order(order["public_id"])
            if not fresh or fresh.get("paid_charge_id") != f"wallet:{order['public_id']}":
                # we charged, but the order closed in between: give the money back (once)
                try:
                    await wallet.credit(order["user_id"], order["total_toman"], idem_key=f"revert:{order['public_id']}",
                                        kind="REFUND", ref=order["public_id"], actor="system",
                                        note="order closed during payment; debit reverted")
                except wallet.DuplicateTransaction:
                    pass
                log.warning("wallet debit reverted for closed order %s", order["public_id"])
        return updated

    async def refund(self, order: dict, *, actor: str = "system", reason: str = "") -> bool:
        """Refund a wallet-paid order to the wallet. Only one refund can ever start / be credited."""
        if order.get("payment_method") != "wallet" or not order.get("paid_at"):
            return False
        reason = (reason or "").strip()
        if not reason:
            return False
        started = await orders.transition(
            order["public_id"], S.REFUND_PENDING, from_states=[S.PAID, S.PROCESSING, S.FAILED, S.FULFILLED],
            note=f"refund by {actor}: {reason}"[:300])
        if not started:
            return False
        try:
            await wallet.refund_order(order["user_id"], order["total_toman"], order["public_id"], actor, note=reason[:200])
        except wallet.DuplicateTransaction:
            pass                                    # credited before (crash after credit): just finish the state change
        except Exception as exc:
            log.exception("wallet refund failed")
            await orders.transition(order["public_id"], S.FAILED, from_states=[S.REFUND_PENDING],
                                    set_fields={"fulfillment_error": f"refund failed: {type(exc).__name__}"}, note="refund failed")
            return False
        await orders.transition(order["public_id"], S.REFUNDED, from_states=[S.REFUND_PENDING],
                                set_fields={"payment_status": "REFUNDED", "refund_reason": reason[:200],
                                            "refunded_by": actor}, note="refunded to wallet")
        return True


async def recover_orphan_debits(limit: int = 100) -> int:
    """A debit exists but its order was closed without ever being paid (crash between debit and PAID): give it back, once.
    Runs in the background; credit uses the same idem key as the in-line revert, so it can never double-refund."""
    from datetime import timedelta
    from db import get_db
    n = 0
    since = utcnow() - timedelta(hours=48)
    async for row in get_db().wallet_ledger.find({"type": "PURCHASE", "created_at": {"$gt": since}}).limit(limit):
        if row.get("status") == "PENDING":
            continue
        order = await orders.get_order(row.get("ref") or "")
        if not order or order.get("paid_at") or order["status"] not in (S.CANCELLED, S.FAILED):
            continue
        try:
            await wallet.credit(order["user_id"], int(order["total_toman"]), idem_key=f"revert:{order['public_id']}",
                                kind="REFUND", ref=order["public_id"], actor="system",
                                note="unpaid closed order; debit reverted")
            n += 1
            log.warning("orphan wallet debit reverted for order %s", order["public_id"])
        except wallet.DuplicateTransaction:
            pass
    return n
