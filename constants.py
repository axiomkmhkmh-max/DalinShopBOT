"""Order state machine. Only transitions listed here are allowed."""
from __future__ import annotations


class OrderStatus:
    PENDING_PAYMENT = "PENDING_PAYMENT"
    PAID = "PAID"
    PROCESSING = "PROCESSING"
    FULFILLED = "FULFILLED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    REFUND_PENDING = "REFUND_PENDING"
    REFUNDED = "REFUNDED"


S = OrderStatus
ALL_STATUSES = [
    S.PENDING_PAYMENT, S.PAID, S.PROCESSING, S.FULFILLED,
    S.FAILED, S.CANCELLED, S.REFUND_PENDING, S.REFUNDED,
]

TRANSITIONS: dict[str, set[str]] = {
    S.PENDING_PAYMENT: {S.PAID, S.CANCELLED, S.FAILED},
    S.PAID: {S.PROCESSING, S.REFUND_PENDING},
    S.PROCESSING: {S.FULFILLED, S.FAILED, S.REFUND_PENDING},
    S.FAILED: {S.PROCESSING, S.REFUND_PENDING},   # retry fulfillment or refund (paid orders only; see service)
    S.FULFILLED: {S.REFUND_PENDING},
    S.REFUND_PENDING: {S.REFUNDED, S.FAILED},
    S.CANCELLED: set(),
    S.REFUNDED: set(),
}


def can_transition(src: str, dst: str) -> bool:
    return dst in TRANSITIONS.get(src, set())


def sources_for(dst: str) -> list[str]:
    return [s for s, targets in TRANSITIONS.items() if dst in targets]


STATUS_FA = {
    S.PENDING_PAYMENT: "در انتظار پرداخت",
    S.PAID: "پرداخت‌شده",
    S.PROCESSING: "در حال پردازش",
    S.FULFILLED: "تحویل‌شده",
    S.FAILED: "ناموفق (در حال بررسی)",
    S.CANCELLED: "لغو‌شده",
    S.REFUND_PENDING: "در انتظار بازپرداخت",
    S.REFUNDED: "بازپرداخت‌شده",
}

# Roles & permissions for the admin dashboard
PERMISSIONS: dict[str, set[str]] = {
    "owner": {"*"},
    "administrator": {"products", "users", "orders", "pricing", "suppliers", "support", "audit", "settings_basic"},
    "finance": {"payments", "wallets_adjust", "refunds", "reports", "orders_view", "pricing_view"},
    "support": {"support", "orders_view"},
    "order_manager": {"orders", "orders_view"},
}


def has_permission(role: str, perm: str) -> bool:
    perms = PERMISSIONS.get(role, set())
    return "*" in perms or perm in perms


# Technical names used in the requirements (created / awaiting_payment / paid / processing / delivered /
# pending_manual_delivery / failed / refunded). The stored statuses stay as they are (existing data + tests); this is
# the mapping shown in the dashboard and used in reports. "created" is the first entry of an order's history.
def canonical_status(order: dict) -> str:
    st = order.get("status")
    if st == S.PROCESSING and order.get("fulfillment_status") == "MANUAL_PENDING":
        return "pending_manual_delivery"
    return {
        S.PENDING_PAYMENT: "awaiting_payment", S.PAID: "paid", S.PROCESSING: "processing", S.FULFILLED: "delivered",
        S.FAILED: "failed", S.CANCELLED: "cancelled", S.REFUND_PENDING: "refund_pending", S.REFUNDED: "refunded",
    }.get(st, str(st))
