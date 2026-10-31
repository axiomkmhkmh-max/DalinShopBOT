"""Wallet with an append-only ledger.

Guarantees
  * balances are integer Toman; every change has exactly one ledger row (unique idem_key)
  * debit uses an atomic guarded update (balance >= amount) => no negative balance, no double spend
  * the same idem_key can never apply twice (duplicate transaction / duplicate refund protection)
  * reconcile() recomputes the balance from the ledger and compares
NOTE: MongoDB multi-document transactions are not assumed (single-node Railway Mongo). Ordering is:
  guarded balance update -> ledger insert -> (on ledger failure) compensating update. See README.
"""
from __future__ import annotations

import logging

from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError

from db import get_db
from util import new_id, utcnow

log = logging.getLogger(__name__)


class InsufficientFunds(Exception):
    pass


class DuplicateTransaction(Exception):
    def __init__(self, entry: dict):
        super().__init__("duplicate transaction")
        self.entry = entry


async def get_balance(user_id: int) -> int:
    w = await get_db().wallets.find_one({"user_id": user_id})
    return int(w["balance"]) if w else 0


async def _ensure_wallet(user_id: int) -> None:
    try:
        await get_db().wallets.update_one(
            {"user_id": user_id},
            {"$setOnInsert": {"user_id": user_id, "balance": 0, "created_at": utcnow()}},
            upsert=True,
        )
    except DuplicateKeyError:
        pass


async def _apply(user_id: int, signed: int, kind: str, idem_key: str, ref: str, actor: str, note: str) -> dict:
    if not isinstance(signed, int) or signed == 0:
        raise ValueError("amount must be a non-zero int")
    db = get_db()
    existing = await db.wallet_ledger.find_one({"idem_key": idem_key})
    if existing:
        raise DuplicateTransaction(existing)
    await _ensure_wallet(user_id)
    query = {"user_id": user_id}
    if signed < 0:
        query["balance"] = {"$gte": -signed}
    wallet = await db.wallets.find_one_and_update(
        query, {"$inc": {"balance": signed}}, return_document=ReturnDocument.AFTER
    )
    if wallet is None:
        raise InsufficientFunds()
    entry = {
        "public_id": new_id(),
        "idem_key": idem_key,
        "user_id": user_id,
        "type": kind,
        "amount": signed,
        "balance_after": int(wallet["balance"]),
        "ref": ref,
        "actor": actor,
        "note": note,
        "created_at": utcnow(),
    }
    try:
        await db.wallet_ledger.insert_one(entry)
    except DuplicateKeyError:
        # lost a race with an identical request: undo our balance change, report duplicate
        await db.wallets.update_one({"user_id": user_id}, {"$inc": {"balance": -signed}})
        raise DuplicateTransaction(await db.wallet_ledger.find_one({"idem_key": idem_key}))
    except Exception:
        await db.wallets.update_one({"user_id": user_id}, {"$inc": {"balance": -signed}})
        log.exception("ledger insert failed; balance change reverted")
        raise
    return entry


async def credit(user_id: int, amount: int, *, idem_key: str, kind: str = "CREDIT", ref: str = "", actor: str = "system", note: str = "") -> dict:
    if amount <= 0:
        raise ValueError("credit amount must be positive")
    return await _apply(user_id, amount, kind, idem_key, ref, actor, note)


async def debit(user_id: int, amount: int, *, idem_key: str, kind: str = "DEBIT", ref: str = "", actor: str = "system", note: str = "") -> dict:
    if amount <= 0:
        raise ValueError("debit amount must be positive")
    return await _apply(user_id, -amount, kind, idem_key, ref, actor, note)


async def refund_order(user_id: int, amount: int, order_id: str, actor: str) -> dict:
    """One refund per order, enforced by the idem key."""
    return await credit(user_id, amount, idem_key=f"refund:{order_id}", kind="REFUND", ref=order_id, actor=actor)


async def reconcile(user_id: int) -> dict:
    db = get_db()
    total = 0
    async for row in db.wallet_ledger.find({"user_id": user_id}):
        total += int(row["amount"])
    balance = await get_balance(user_id)
    return {"user_id": user_id, "ledger_sum": total, "balance": balance, "ok": total == balance}
