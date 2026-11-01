"""Auto-pricing engine.

Inputs
  * base price of a product (one of):
      - "telegram": Star cost reported by Telegram (Gift price from getAvailableGifts, Premium package price).
                    Stored on the product by telegram_supply.sync_supply().
      - "manual":   amount + currency typed by the admin (IRT / IRR / USD / XTR).
  * exchange rate USD -> Toman: from the optional rate API (RATE_API_URL ...) or typed by hand (manual).
  * "XTR" (Telegram Stars) is converted with   stars_cost_usd (what 1 Star costs YOU) x USD rate.
Output: one quote per product in `price_quotes` (final Toman price, Stars price, cost, profit, timestamps).

A quote is only usable while ALL its inputs are fresh:
  * automatic inputs (rate API, Telegram data): max_age_minutes
  * rate typed by hand: manual_rate_max_age_minutes
Stale/failed => PriceUnavailable => the product cannot be bought. The last known price is NEVER shown as live.
"""
from __future__ import annotations

import logging
from datetime import timedelta
from decimal import Decimal

from db import get_db
from money import MoneyError, compute_price, stars_within_profit_cap, to_decimal
import settings as st
from pricing_providers import ProviderError, RateProvider
from util import as_aware, redact, utcnow

log = logging.getLogger(__name__)


class PriceUnavailable(Exception):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def _needs_rate(currency: str) -> bool:
    return currency.upper() not in ("IRT", "IRR")


# ---------------------------------------------------------------- rates
async def set_manual_rate(currency: str, value) -> None:
    """Admin typed the rate by hand. Always stamps a new time (re-saving the same value renews it)."""
    rate = to_decimal(value)
    if rate <= 0:
        raise MoneyError("rate must be positive")
    now = utcnow()
    await get_db().exchange_rates.update_one(
        {"_id": currency.upper()},
        {"$set": {"toman_per_unit": str(rate), "fetched_at": now, "source": "manual",
                  "last_error": None, "last_attempt_at": now}},
        upsert=True,
    )


async def refresh_rates(rate_provider: RateProvider | None, currencies: set[str]) -> dict[str, str]:
    """Returns {currency: 'ok' | error text}. A failed fetch keeps the old doc untouched (it will age out)."""
    result: dict[str, str] = {}
    if rate_provider is None or not rate_provider.configured:
        return {cur: "not configured (the manual rate is used)" for cur in sorted(currencies)}
    db = get_db()
    for cur in sorted(currencies):
        try:
            rate = await rate_provider.get_rate(cur)
            await db.exchange_rates.update_one(
                {"_id": cur},
                {"$set": {
                    "toman_per_unit": str(rate.toman_per_unit),
                    "fetched_at": rate.fetched_at,
                    "source": rate.source,
                    "last_error": None,
                    "last_attempt_at": utcnow(),
                }},
                upsert=True,
            )
            result[cur] = "ok"
        except ProviderError as exc:
            msg = redact(exc)
            await db.exchange_rates.update_one(
                {"_id": cur}, {"$set": {"last_error": msg, "last_attempt_at": utcnow()}}, upsert=True
            )
            result[cur] = f"error: {msg}"
    return result


async def get_rate_doc(currency: str) -> dict:
    """Rate document {toman_per_unit, fetched_at, source} for a currency, or PriceUnavailable.
    XTR is derived: stars_cost_usd x USD rate."""
    cur = currency.upper()
    db = get_db()
    if cur == "XTR":
        usd = await db.exchange_rates.find_one({"_id": "USD"})
        if not usd or not usd.get("toman_per_unit"):
            raise PriceUnavailable("no USD rate")
        cost_usd = await st.get_setting("stars_cost_usd")
        if not cost_usd:
            raise PriceUnavailable("stars_cost_usd is not set")
        return {
            "toman_per_unit": str(to_decimal(cost_usd) * to_decimal(usd["toman_per_unit"])),
            "fetched_at": usd.get("fetched_at"),
            "source": usd.get("source"),
        }
    doc = await db.exchange_rates.find_one({"_id": cur})
    if not doc or not doc.get("toman_per_unit"):
        raise PriceUnavailable(f"no exchange rate for {currency}")
    return doc


# ---------------------------------------------------------------- quotes
async def _store_quote(product: dict, base_amount, base_currency: str, base_fetched_at, rate_doc) -> dict:
    profit = product.get("profit_toman") or await st.get_setting("profit_toman")
    breakdown = compute_price(
        base_amount,
        base_currency,
        to_decimal(rate_doc["toman_per_unit"]) if rate_doc else None,
        profit,
        await st.get_int("rounding_step"),
        await st.get_setting("rounding_mode"),
    )
    stars_usd = await st.get_setting("stars_usd_value")
    usd_rate = await get_db().exchange_rates.find_one({"_id": "USD"})
    toman_per_star = stars = stars_equiv = stars_profit = receivable = None
    if stars_usd and usd_rate and usd_rate.get("toman_per_unit"):
        toman_per_star = to_decimal(stars_usd) * to_decimal(usd_rate["toman_per_unit"])
        # Stars are what the customer really pays: keep THAT amount inside the 7000 Toman profit cap.
        stars = stars_within_profit_cap(breakdown.final_toman, toman_per_star, breakdown.cost_toman)
        stars_equiv = Decimal(stars) * toman_per_star
        stars_profit = stars_equiv - breakdown.cost_toman
        payout = await st.get_setting("stars_payout_usd_value")
        if payout:
            receivable = str((Decimal(stars) * to_decimal(payout) * to_decimal(usd_rate["toman_per_unit"])).quantize(Decimal("1")))
    doc = {
        "product_id": product["public_id"],
        "base_amount": str(breakdown.base_amount),
        "base_currency": breakdown.base_currency,
        "rate_toman_per_unit": str(breakdown.rate_toman_per_unit) if breakdown.rate_toman_per_unit else None,
        "cost_toman": str(breakdown.cost_toman),
        "profit_toman": str(breakdown.profit_toman),
        "effective_profit_toman": str(breakdown.effective_profit_toman),
        "final_toman": breakdown.final_toman,
        "toman_per_star": str(toman_per_star) if toman_per_star else None,
        "stars_amount": stars,
        "stars_equiv_toman": str(stars_equiv.quantize(Decimal("1"))) if stars_equiv is not None else None,
        "stars_profit_toman": str(stars_profit.quantize(Decimal("1"))) if stars_profit is not None else None,
        "seller_receivable_toman": receivable,   # what the seller actually gets (informational)
        "base_fetched_at": base_fetched_at,
        "rate_fetched_at": rate_doc["fetched_at"] if rate_doc else None,
        "rate_source": rate_doc.get("source") if rate_doc else None,
        "usd_rate_fetched_at": usd_rate.get("fetched_at") if usd_rate else None,
        "usd_rate_source": usd_rate.get("source") if usd_rate else None,
        "updated_at": utcnow(),
        "status": "OK",
        "error": None,
    }
    await get_db().price_quotes.update_one({"product_id": product["public_id"]}, {"$set": doc}, upsert=True)
    return doc


async def _mark_quote_error(product_id: str, error: str) -> None:
    await get_db().price_quotes.update_one(
        {"product_id": product_id},
        {"$set": {"error": redact(error), "last_attempt_at": utcnow()}},
        upsert=True,
    )


async def recompute_product(product: dict) -> None:
    """Recompute one product from cached inputs (no network)."""
    pricing = product.get("pricing") or {}
    source = pricing.get("source") or "manual"
    try:
        if source == "telegram":
            sup = product.get("supply") or {}
            if not sup.get("stars_cost") or not sup.get("checked_at"):
                raise PriceUnavailable("Telegram price not synced yet")
            amount, currency, fetched = sup["stars_cost"], "XTR", sup["checked_at"]
        elif source == "manual":
            amount, currency = pricing["manual_amount"], pricing["manual_currency"]
            fetched = None  # manual product prices are intentional, they do not expire
        else:
            raise PriceUnavailable(f"unknown price source: {source}")
        rate_doc = await get_rate_doc(currency) if _needs_rate(currency) else None
        await _store_quote(product, amount, currency, fetched, rate_doc)
    except (PriceUnavailable, MoneyError, KeyError) as exc:
        await _mark_quote_error(product["public_id"], str(exc))


async def custom_stars_quote(stars: int) -> dict:
    """Price for a customer-typed number of Stars. Same formula as the packages (cost + profit, rounded, profit never
    above the 7000 Toman cap, Stars the customer really pays kept inside the cap). Nothing is stored: the order freezes it.
    Raises PriceUnavailable when any input (USD rate, Star cost, Star value) is missing or stale."""
    if isinstance(stars, bool) or not isinstance(stars, int) or stars < 1:
        raise PriceUnavailable("invalid amount")
    db = get_db()
    cost_doc = await get_rate_doc("XTR")                     # Toman cost of ONE Star = stars_cost_usd x USD rate
    usd = await db.exchange_rates.find_one({"_id": "USD"})
    limit = timedelta(minutes=await st.get_int("manual_rate_max_age_minutes" if usd.get("source") == "manual" else "max_age_minutes"))
    fetched = as_aware(usd.get("fetched_at"))
    if fetched is None or utcnow() - fetched > limit:
        raise PriceUnavailable("USD rate is stale")
    stars_usd = await st.get_setting("stars_usd_value")
    if not stars_usd:
        raise PriceUnavailable("stars_usd_value is not set")
    breakdown = compute_price(stars, "XTR", to_decimal(cost_doc["toman_per_unit"]), await st.get_setting("profit_toman"),
                              await st.get_int("rounding_step"), await st.get_setting("rounding_mode"))
    toman_per_star = to_decimal(stars_usd) * to_decimal(usd["toman_per_unit"])
    stars_amount = stars_within_profit_cap(breakdown.final_toman, toman_per_star, breakdown.cost_toman)
    return {
        "custom": True, "base_amount": str(breakdown.base_amount), "base_currency": "XTR",
        "rate_toman_per_unit": str(breakdown.rate_toman_per_unit), "cost_toman": str(breakdown.cost_toman),
        "profit_toman": str(breakdown.profit_toman), "effective_profit_toman": str(breakdown.effective_profit_toman),
        "final_toman": breakdown.final_toman, "toman_per_star": str(toman_per_star), "stars_amount": stars_amount,
        "stars_profit_toman": str((Decimal(stars_amount) * toman_per_star - breakdown.cost_toman).quantize(Decimal("1"))),
        "usd_rate_fetched_at": fetched, "usd_rate_source": usd.get("source"), "updated_at": utcnow(),
    }


async def refresh_all(rate_provider: RateProvider | None = None, fetch: bool = True) -> dict:
    """One cycle. fetch=True also asks the rate API; fetch=False only recomputes from cached inputs
    (after a settings change, a manual rate, a Telegram sync...). Telegram data is synced separately
    (telegram_supply.sync_supply) BEFORE this runs."""
    db = get_db()
    products = [p async for p in db.products.find({}) if not p.get("custom_amount")]   # custom-amount product is priced per order
    report: dict = {"rates": {}, "products": {}}
    if fetch:
        currencies = {"USD"}   # Stars price for the customer always needs the USD rate
        for p in products:
            pr = p.get("pricing") or {}
            if (pr.get("source") or "manual") == "manual":
                cur = (pr.get("manual_currency") or "IRT").upper()
                if _needs_rate(cur) and cur != "XTR":
                    currencies.add(cur)
        report["rates"] = await refresh_rates(rate_provider, currencies)
    for p in products:
        await recompute_product(p)
        q = await db.price_quotes.find_one({"product_id": p["public_id"]})
        report["products"][p["public_id"]] = "ok" if q and not q.get("error") else ((q or {}).get("error") or "error")
    return report


async def get_valid_quote(product_id: str) -> dict:
    """Return a quote only if every input is fresh. Otherwise raise PriceUnavailable."""
    db = get_db()
    q = await db.price_quotes.find_one({"product_id": product_id})
    if not q or q.get("status") != "OK" or not q.get("final_toman"):
        raise PriceUnavailable("no valid quote")
    auto_age = timedelta(minutes=await st.get_int("max_age_minutes"))
    manual_age = timedelta(minutes=await st.get_int("manual_rate_max_age_minutes"))
    now = utcnow()
    for field, source in (("base_fetched_at", "telegram"),
                          ("rate_fetched_at", q.get("rate_source")),
                          ("usd_rate_fetched_at", q.get("usd_rate_source"))):
        ts = as_aware(q.get(field))
        if ts is None:
            continue
        limit = manual_age if source == "manual" else auto_age
        if now - ts > limit:
            raise PriceUnavailable(f"{field} is stale")
    # q["error"] set => the LAST refresh failed after this quote was stored. The quote may still be used while
    # every input is inside its max age (checked above); it can never outlive them.
    return q
