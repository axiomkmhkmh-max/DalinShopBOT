"""REAL supplier tests - skipped unless you opt in. They talk to the real Stard API with YOUR test key (sk_test_...).
  Windows:  set RUN_LIVE_SUPPLIER_TESTS=1 & set STARS_SUPPLIER_API_KEY=sk_test_xxx & set LIVE_TEST_RECIPIENT=@yourusername & pytest test_stard_live.py -s
  Mac/Linux: RUN_LIVE_SUPPLIER_TESTS=1 STARS_SUPPLIER_API_KEY=sk_test_xxx LIVE_TEST_RECIPIENT=@yourusername pytest test_stard_live.py -s
They NEVER run with an sk_live_ key (no real money is ever spent by a test). A passing run still does not mean Telegram Stars
were really delivered: sk_test_ orders are simulated by the supplier. Real delivery is proven only by a real order."""
import asyncio
import os

import pytest

from stars_supplier import NormalizedStatus as N, StardAdapter

KEY = os.environ.get("STARS_SUPPLIER_API_KEY", "")
pytestmark = pytest.mark.skipif(os.environ.get("RUN_LIVE_SUPPLIER_TESTS") != "1" or not KEY.startswith("sk_test_"),
                                reason="opt-in: RUN_LIVE_SUPPLIER_TESTS=1 and an sk_test_ key are required")


def adapter():
    return StardAdapter(KEY, os.environ.get("STARS_SUPPLIER_API_BASE", "https://stard-market.ir/api/v1"))


async def test_ping_and_balance():
    a = adapter()
    assert (await a.ping())["ok"]
    assert await a.balance() is not None


async def test_quote_for_50_stars():
    q = await adapter().quote(50)
    assert q.amount_toman > 0


async def test_simulated_order_reaches_completed():
    recipient = os.environ.get("LIVE_TEST_RECIPIENT", "")
    if not recipient:
        pytest.skip("set LIVE_TEST_RECIPIENT=@yourusername")
    a = adapter()
    key = f"live-test-{os.urandom(6).hex()}"
    info = await a.create_order(idempotency_key=key, quantity=50, recipient=recipient)
    again = await a.create_order(idempotency_key=key, quantity=50, recipient=recipient)
    assert again.ref == info.ref                                  # replay-safe: the same key did not create a second order
    for _ in range(12):
        await asyncio.sleep(5)
        if (await a.get_order(info.ref)).status is N.DELIVERED:
            return
    pytest.fail("simulated order did not complete within 60 seconds")
