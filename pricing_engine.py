"""Auto-pricing engine.

Flow (scheduled, interval from admin settings):
  1. fetch each needed FX rate ONCE (not per product)  -> exchange_rates
  2. fetch base price per provider-sourced product      -> (in-memory)
  3. compute final price with Decimal                   -> price_quotes
A quote is only usable while all of its inputs are fresh (max_age_minutes).
Stale/failed => PriceUnavailable => the product cannot be bought. The last
known price is NEVER shown as a live price.
"""
from __future__ import annotations

import logging
from datetime import timedelta
from decimal import Decimal

from db import get_db
from money import MoneyError, compute_price, stars_for_toman, to_decimal
import settings as st
from pricing_providers import PriceProvider, ProviderError, RateProvider
from util import as_aware, utcnow

log = logging.getLogger(__name__)


class PriceUnavailable(Exception):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def _needs_rate(currency: str) -> bool:
    return currency.upper() not in ("IRT", "IRR")


async def refresh_rates(rate_provider: RateProvider, currencies: set[str]) -> dict[str, str]:
    """Returns {currency: 'ok' | error}. A failed fetch keeps the old doc untouched (it will age out)."""
    db = get_db()
    result: dict[str, str] = {}
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
            await db.exchange_rates.update_one(
                {"_id": cur}, {"$set": {"last_error": str(exc), "last_attempt_at": utcnow()}}, upsert=True
            )
            result[cur] = f"error: {exc}"
    return result


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
    toman_per_star = None
    stars = None
    receivable = None
    if stars_usd and usd_rate and usd_rate.get("toman_per_unit"):
        toman_per_star = to_decimal(stars_usd) * to_decimal(usd_rate["toman_per_unit"])
        stars = stars_for_toman(breakdown.final_toman, toman_per_star)
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
        "seller_receivable_toman": receivable,   # what the seller actually gets (informational)
        "base_fetched_at": base_fetched_at,
        "rate_fetched_at": rate_doc["fetched_at"] if rate_doc else None,
        "updated_at": utcnow(),
        "status": "OK",
        "error": None,
    }
    await get_db().price_quotes.update_one({"product_id": product["public_id"]}, {"$set": doc}, upsert=True)
    return doc


async def _mark_quote_error(product_id: str, error: str) -> None:
    await get_db().price_quotes.update_one(
        {"product_id": product_id},
        {"$set": {"error": error, "last_attempt_at": utcnow()}},
        upsert=True,
    )


async def recompute_product(product: dict, base_override=None) -> None:
    """Recompute from cached inputs (no network). base_override = (BasePrice) for provider-sourced products."""
    db = get_db()
    pricing = product.get("pricing") or {}
    try:
        if base_override is not None:
            amount, currency, fetched = base_override.amount, base_override.currency, base_override.fetched_at
        elif pricing.get("source") == "manual":
            amount, currency = pricing["manual_amount"], pricing["manual_currency"]
            fetched = None  # manual prices are intentional, they do not expire
        else:
            cached = await db.price_quotes.find_one({"product_id": product["public_id"]})
            if not cached or cached.get("base_amount") is None:
                raise PriceUnavailable("no cached provider price")
            amount, currency, fetched = cached["base_amount"], cached["base_currency"], cached["base_fetched_at"]
        rate_doc = None
        if _needs_rate(currency):
            rate_doc = await db.exchange_rates.find_one({"_id": currency.upper()})
            if not rate_doc or not rate_doc.get("toman_per_unit"):
                raise PriceUnavailable(f"no exchange rate for {currency}")
        await _store_quote(product, amount, currency, fetched, rate_doc)
    except (PriceUnavailable, MoneyError, KeyError) as exc:
        await _mark_quote_error(product["public_id"], str(exc))


async def refresh_all(price_provider: PriceProvider, rate_provider: RateProvider, fetch: bool = True) -> dict:
    """One scheduled cycle. fetch=False only recomputes from cache (e.g. after profit change)."""
    db = get_db()
    products = [p async for p in db.products.find({"enabled": True})]
    report = {"rates": {}, "products": {}}
    if fetch:
        currencies: set[str] = set()
        for p in products:
            pr = p.get("pricing") or {}
            if pr.get("source") == "manual":
                if _needs_rate(pr.get("manual_currency", "IRT")):
                    currencies.add(pr["manual_currency"].upper())
            else:
                currencies.add("__provider__")
        provider_needed = "__provider__" in currencies
        currencies.discard("__provider__")
        # Stars pricing always needs USD
        currencies.add("USD")
        report["rates"] = await refresh_rates(rate_provider, currencies)
        fetched_prices = {}
        if provider_needed:
            for p in products:
                pr = p.get("pricing") or {}
                if pr.get("source") != "manual":
                    ref = pr.get("provider_ref")
                    if not ref:
                        continue
                    try:
                        fetched_prices[p["public_id"]] = await price_provider.get_base_price(ref)
                    except ProviderError as exc:
                        report["products"][p["public_id"]] = f"error: {exc}"
                        await _mark_quote_error(p["public_id"], f"price fetch failed: {exc}")
        # rate_provider currencies for provider-sourced products are learned after the first price fetch
        extra = {bp.currency.upper() for bp in fetched_prices.values() if _needs_rate(bp.currency)}
        missing = extra - set(report["rates"])
        if missing:
            report["rates"].update(await refresh_rates(rate_provider, missing))
        for p in products:
            if p["public_id"] in fetched_prices:
                await recompute_product(p, base_override=fetched_prices[p["public_id"]])
                report["products"].setdefault(p["public_id"], "ok")
            elif (p.get("pricing") or {}).get("source") == "manual":
                await recompute_product(p)
                report["products"].setdefault(p["public_id"], "ok")
    else:
        for p in products:
            await recompute_product(p)
    return report


async def get_valid_quote(product_id: str) -> dict:
    """Return a quote only if every input is fresh. Otherwise raise PriceUnavailable."""
    db = get_db()
    q = await db.price_quotes.find_one({"product_id": product_id})
    if not q or q.get("status") != "OK" or not q.get("final_toman"):
        raise PriceUnavailable("no valid quote")
    max_age = timedelta(minutes=await st.get_int("max_age_minutes"))
    now = utcnow()
    for field in ("base_fetched_at", "rate_fetched_at"):
        ts = as_aware(q.get(field))
        if ts is not None and now - ts > max_age:
            raise PriceUnavailable(f"{field} is stale")
    if q.get("error"):
        # last refresh attempt failed AFTER this quote was stored; the quote is still within max age, so allow
        # it, but never if the inputs are older than max_age (checked above).
        pass
    return q
