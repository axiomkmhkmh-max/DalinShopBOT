from decimal import Decimal

import pytest

from money import (MAX_PROFIT_TOMAN, MoneyError, clamp_profit, compute_price, rial_to_toman,
                       round_to_step, stars_for_toman, to_decimal, validate_profit)


def test_price_formula_usd():
    b = compute_price("2", "USD", "100000", "5000", 1000, "up")   # test numbers only
    assert b.cost_toman == Decimal("200000")
    assert b.final_toman == 205000
    assert b.effective_profit_toman == Decimal("5000")


def test_rial_to_toman_is_exact():
    assert rial_to_toman("1250000") == Decimal("125000")
    b = compute_price("1250000", "IRR", None, "0", 1000, "up")
    assert b.cost_toman == Decimal("125000") and b.final_toman == 125000


def test_toman_base_is_not_converted_again():
    b = compute_price("90000", "IRT", "999999", "3000", 1000, "up")
    assert b.cost_toman == Decimal("90000") and b.final_toman == 93000


def test_profit_validation_and_clamp():
    assert validate_profit("0") == 0 and validate_profit("7000") == MAX_PROFIT_TOMAN
    for bad in ("7001", "-1", "abc"):
        with pytest.raises(MoneyError):
            validate_profit(bad)
    assert clamp_profit("99999") == Decimal("7000")
    assert clamp_profit("-5") == Decimal("0")


def test_profit_never_exceeds_cap_even_with_rounding():
    # cost 200300 + 7000 = 207300; rounding UP to 5000 would give 210000 (profit 9700) -> must be reduced
    b = compute_price("2003", "USD", "100", "7000", 5000, "up")
    assert b.final_toman == 205000
    assert b.effective_profit_toman <= MAX_PROFIT_TOMAN


def test_never_below_cost():
    b = compute_price("2003", "USD", "100", "0", 1000, "down")   # cost 200300
    assert b.final_toman >= 200300


def test_even_huge_step_respects_cap_and_cost():
    b = compute_price("2003", "USD", "100", "7000", 100000, "up")
    assert Decimal(b.final_toman) - b.cost_toman <= MAX_PROFIT_TOMAN
    assert b.final_toman >= b.cost_toman


def test_effective_profit_always_within_range_sweep():
    for base in ("1", "2.37", "13.999", "250"):
        for profit in ("0", "1", "3500", "7000", "100000"):
            for step in (1, 100, 1000, 5000, 50000):
                for mode in ("up", "down", "nearest"):
                    b = compute_price(base, "USD", "61234.5", profit, step, mode)
                    assert Decimal(0) <= b.effective_profit_toman <= MAX_PROFIT_TOMAN + Decimal("0.0001"), (base, profit, step, mode)
                    assert b.final_toman >= b.cost_toman


def test_rounding_modes():
    assert round_to_step(Decimal("205001"), 1000, "up") == 206000
    assert round_to_step(Decimal("205999"), 1000, "down") == 205000
    assert round_to_step(Decimal("205500"), 1000, "nearest") == 206000
    with pytest.raises(MoneyError):
        round_to_step(Decimal("1"), 0, "up")


def test_floats_are_rejected():
    with pytest.raises(MoneyError):
        to_decimal(0.1)
    with pytest.raises(MoneyError):
        compute_price(1.5, "USD", "100", "0")


def test_stars_ceiling():
    assert stars_for_toman(205000, "2000") == 103
    assert stars_for_toman(1, "2000") == 1
