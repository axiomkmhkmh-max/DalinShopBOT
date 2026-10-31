"""Small indirection so the web layer does not import background loops at module import time."""
from __future__ import annotations

from config import get_config
import settings as st
import pricing_engine as engine
from pricing_providers import ProviderError, build_price_provider, build_rate_provider


async def recompute_all() -> None:
    cfg = get_config()
    try:
        pp = build_price_provider(await st.get_setting("price_provider"), cfg)
        rp = build_rate_provider(await st.get_setting("rate_provider"), cfg)
    except ProviderError:
        return
    await engine.refresh_all(pp, rp, fetch=False)      # cache-only: no API calls


async def refresh_now():
    from background import run_pricing_cycle
    return await run_pricing_cycle(force=True)
