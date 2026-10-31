"""Seed demo data for LOCAL development: categories, products, manual supplier.
Prices are NOT set here: they come from the pricing engine (manual base price + exchange rate provider).
Usage:  python seed.py
"""
import asyncio

import db as dbmod
from config import get_config
import catalog


async def main():
    cfg = get_config()
    db = await dbmod.init_db(cfg.mongodb_uri, cfg.mongodb_db)
    await db.suppliers.update_one({"key": "manual"}, {"$set": {"name": "تحویل دستی", "type": "manual", "enabled": True}}, upsert=True)
    if await db.categories.count_documents({}) == 0:
        cid = await catalog.save_category("Telegram Premium")
        await catalog.save_product({
            "name": "Telegram Premium ۳ ماهه", "description": "اشتراک ۳ ماهه", "kind": "premium", "category_id": cid,
            "enabled": False,  # enable it in the dashboard after setting its base price
            "pricing": {"source": "manual", "manual_amount": "1", "manual_currency": "USD"},
            "supplier_key": "manual", "digital_in_telegram": True})
        print("demo category/product created (product is DISABLED; set base price and enable it in the dashboard)")
    print("seed done")


asyncio.run(main())
