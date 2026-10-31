"""Small indirection so the web layer does not import background loops at module import time."""
from __future__ import annotations

import pricing_engine as engine


async def recompute_all() -> None:
    await engine.refresh_all(None, fetch=False)      # cache-only: no API calls


async def refresh_now():
    from background import run_pricing_cycle
    return await run_pricing_cycle(force=True)
