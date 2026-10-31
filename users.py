from __future__ import annotations

from pymongo.errors import DuplicateKeyError

from db import get_db
from util import new_id, utcnow


async def upsert_user(tg_user, referrer_code: str | None = None) -> dict:
    db = get_db()
    now = utcnow()
    existing = await db.users.find_one({"telegram_id": tg_user.id})
    if existing:
        await db.users.update_one(
            {"telegram_id": tg_user.id},
            {"$set": {"username": tg_user.username, "first_name": tg_user.first_name, "last_seen": now}},
        )
        return existing
    referred_by = None
    if referrer_code:
        ref = await db.users.find_one({"ref_code": referrer_code})
        if ref and ref["telegram_id"] != tg_user.id:
            referred_by = ref["telegram_id"]
    doc = {
        "telegram_id": tg_user.id,
        "username": tg_user.username,
        "first_name": tg_user.first_name,
        "ref_code": new_id()[:8],
        "referred_by": referred_by,
        "referral_rewarded": False,
        "banned": False,
        "created_at": now,
        "last_seen": now,
    }
    try:
        await db.users.insert_one(doc)
    except DuplicateKeyError:
        return await db.users.find_one({"telegram_id": tg_user.id})
    return doc


async def get_user(telegram_id: int) -> dict | None:
    return await get_db().users.find_one({"telegram_id": telegram_id})
