"""Money helpers. ALL money math uses Decimal. Floats are rejected.

Storage convention (MongoDB):
  * Final prices / wallet balances / ledger amounts: integer Toman (exact).
  * Rates and base prices: Decimal serialized as str (lossless).
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_UP, Decimal, InvalidOperation, localcontext

MAX_PROFIT_TOMAN = Decimal("7000")  # hard business rule: shop profit per product <= 7,000 Toman
ROUNDING_MODES = ("up", "down", "nearest")


class MoneyError(ValueError):
    pass


def to_decimal(value) -> Decimal:
    if isinstance(value, bool) or isinstance(value, float):
        raise MoneyError("float/bool is not allowed for money")
    if isinstance(value, Decimal):
        d = value
    else:
        try:
            d = Decimal(str(value).strip())
        except (InvalidOperation, ValueError) as exc:
            raise MoneyError(f"invalid decimal: {value!r}") from exc
    if not d.is_finite():
        raise MoneyError("non-finite value")
    return d


def rial_to_toman(rial) -> Decimal:
    """1 Toman = 10 Rial. Exact (no rounding here)."""
    return to_decimal(rial) / Decimal(10)


def toman_to_rial(toman) -> Decimal:
    return to_decimal(toman) * Decimal(10)


def validate_profit(profit) -> Decimal:
    """Strict validation for admin input: raises if outside 0..7000."""
    p = to_decimal(profit)
    if p < 0 or p > MAX_PROFIT_TOMAN:
        raise MoneyError("profit must be between 0 and 7000 Toman")
    return p


def clamp_profit(profit) -> Decimal:
    """Defensive clamp used inside the pricing engine."""
    p = to_decimal(profit)
    return max(Decimal(0), min(p, MAX_PROFIT_TOMAN))


def to_int_toman(value) -> int:
    d = to_decimal(value)
    if d != d.to_integral_value():
        raise MoneyError("amount must be a whole number of Toman")
    return int(d)


def round_to_step(amount: Decimal, step: int, mode: str) -> Decimal:
    if step < 1:
        raise MoneyError("rounding step must be >= 1")
    if mode not in ROUNDING_MODES:
        raise MoneyError("invalid rounding mode")
    rounding = {"up": ROUND_CEILING, "down": ROUND_FLOOR, "nearest": ROUND_HALF_UP}[mode]
    with localcontext() as ctx:
        ctx.prec = 40
        units = (amount / Decimal(step)).to_integral_value(rounding=rounding)
        return units * Decimal(step)


@dataclass(frozen=True)
class PriceBreakdown:
    base_amount: Decimal
    base_currency: str
    rate_toman_per_unit: Decimal | None
    cost_toman: Decimal          # base converted to Toman (exact)
    profit_toman: Decimal        # configured (clamped) profit
    raw_toman: Decimal           # cost + profit before rounding
    final_toman: int             # customer price (integer Toman)
    effective_profit_toman: Decimal  # final - cost (<= 7000 ALWAYS)


def compute_price(
    base_amount,
    base_currency: str,
    rate_toman_per_unit,
    profit_toman,
    step: int = 1000,
    mode: str = "up",
) -> PriceBreakdown:
    """final = cost_toman + profit (rounded), with effective profit never above MAX_PROFIT_TOMAN.

    * base_currency IRT: already Toman -> no conversion.
    * base_currency IRR: Rial -> Toman (/10), no FX rate used.
    * any other currency: base_amount * rate (Toman per 1 unit of that currency).
    """
    base = to_decimal(base_amount)
    if base < 0:
        raise MoneyError("base price cannot be negative")
    cur = base_currency.upper()
    rate = None
    if cur == "IRT":
        cost = base
    elif cur == "IRR":
        cost = rial_to_toman(base)
    else:
        if rate_toman_per_unit is None:
            raise MoneyError("exchange rate required")
        rate = to_decimal(rate_toman_per_unit)
        if rate <= 0:
            raise MoneyError("exchange rate must be positive")
        cost = base * rate

    profit = clamp_profit(profit_toman)
    raw = cost + profit
    lower = cost.to_integral_value(rounding=ROUND_CEILING)      # never sell below cost
    upper = (cost + MAX_PROFIT_TOMAN).to_integral_value(rounding=ROUND_FLOOR)  # profit cap
    final = round_to_step(raw, step, mode)
    if final > upper:
        final = round_to_step(upper, step, "down")
        if final > upper:  # pragma: no cover (defensive)
            final = upper
    if final < lower:
        final = round_to_step(lower, step, "up")
        if final > upper:
            final = lower
    final = max(final, lower)
    return PriceBreakdown(
        base_amount=base,
        base_currency=cur,
        rate_toman_per_unit=rate,
        cost_toman=cost,
        profit_toman=profit,
        raw_toman=raw,
        final_toman=int(final),
        effective_profit_toman=Decimal(int(final)) - cost,
    )


def stars_for_toman(final_toman: int, toman_per_star) -> int:
    """Smallest whole number of Stars whose Toman value covers final_toman (>= 1)."""
    tps = to_decimal(toman_per_star)
    if tps <= 0:
        raise MoneyError("toman_per_star must be positive")
    stars = (Decimal(final_toman) / tps).to_integral_value(rounding=ROUND_CEILING)
    return max(1, int(stars))
