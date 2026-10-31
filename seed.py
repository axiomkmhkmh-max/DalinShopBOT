"""Optional LOCAL helper: creates the built-in demo data. On Railway you can do the same from the dashboard
(Products page -> buttons) instead.
Usage:  python seed.py
"""
import asyncio

import catalog
import db as dbmod
import telegram_supply
from config import get_config


async def main():
    cfg = get_config()
    db = await dbmod.init_db(cfg.mongodb_uri, cfg.mongodb_db)
    n = await telegram_supply.create_premium_products()
    print(f"{n} Premium packages created (DISABLED: enable them in the dashboard)")
    if await db.products.count_documents({"kind": "stars"}) == 0:
        cid = await catalog.save_category("Telegram Stars")
        for qty in (50, 100, 500):
            await catalog.save_product({
                "name": f"{qty} Telegram Stars", "description": f"{qty} استار", "kind": "stars", "category_id": cid,
                "enabled": False, "amount": qty, "gift_id": None, "delivery": "manual", "supplier_key": "manual",
                "stock": None, "pricing": {"source": "manual", "manual_amount": str(qty), "manual_currency": "XTR"},
                "digital_in_telegram": True})
        print("Stars packages created (DISABLED)")
    print("seed done. Gifts: use the dashboard button «دریافت گیفت‌ها از تلگرام» (needs the running bot).")


if __name__ == "__main__":
    asyncio.run(main())
