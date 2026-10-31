from constants import OrderStatus as S, can_transition, has_permission


def test_valid_transitions():
    assert can_transition(S.PENDING_PAYMENT, S.PAID)
    assert can_transition(S.PAID, S.PROCESSING)
    assert can_transition(S.PROCESSING, S.FULFILLED)
    assert can_transition(S.FAILED, S.PROCESSING)


def test_invalid_transitions():
    assert not can_transition(S.PENDING_PAYMENT, S.FULFILLED)     # can't deliver without payment
    assert not can_transition(S.PENDING_PAYMENT, S.PROCESSING)
    assert not can_transition(S.FULFILLED, S.PAID)
    assert not can_transition(S.REFUNDED, S.PAID)
    assert not can_transition(S.CANCELLED, S.PAID)


def test_roles():
    assert has_permission("owner", "anything")
    assert has_permission("finance", "wallets_adjust") and has_permission("finance", "refunds")
    assert not has_permission("support", "wallets_adjust")
    assert not has_permission("order_manager", "wallets_adjust")
    assert not has_permission("support", "refunds")
    assert has_permission("order_manager", "orders")
