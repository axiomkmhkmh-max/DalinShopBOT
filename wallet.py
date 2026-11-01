"""Wallet with an append-only ledger.

Guarantees
  * balances are integer Toman; every change has exactly one ledger row (unique idem_key)
  * debit uses an atomic guarded update (balance >= amount) => no negative balance, no double spend
  * the same idem_key can never apply twice (duplicate transaction / duplicate refund protection)
  * reconcile() recomputes the balance from the ledger and compares
NOTE: MongoDB multi-document transactions are not assumed (single-node Railway Mongo). See _apply() for the
  ledger-first, crash-safe ordering and recover_pending() for the recovery job.
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
    """Crash-safe money movement (no multi-document transactions needed):
      1. insert the ledger row as PENDING (unique idem_key = duplicate protection, also across restarts)
      2. ONE atomic wallet update: guarded balance change + remember the idem_key in wallet.applied_keys
         (the key guard makes it impossible to apply the same operation twice, even if step 3 never happened)
      3. mark the ledger row APPLIED with the resulting balance
    If the process dies between the steps, recover_pending() finishes or removes the row; a retry with the same key
    resumes where it stopped. A retry of a COMPLETED operation raises DuplicateTransaction."""
    if not isinstance(signed, int) or signed == 0:
        raise ValueError("amount must be a non-zero int")
    db = get_db()
    await _ensure_wallet(user_id)
    entry = {
        "public_id": new_id(), "idem_key": idem_key, "user_id": user_id, "type": kind, "amount": signed,
        "status": "PENDING", "balance_after": None, "ref": ref, "actor": actor, "note": note, "created_at": utcnow(),
    }
    resumed = False
    try:
        await db.wallet_ledger.insert_one(entry)
    except DuplicateKeyError:
        existing = await db.wallet_ledger.find_one({"idem_key": idem_key})
        if existing is None or existing.get("status") != "PENDING":
            raise DuplicateTransaction(existing or entry)
        resumed = True                      # an earlier attempt stopped half-way (or runs right now): finish it
        entry = existing
    query: dict = {"user_id": user_id, "applied_keys": {"$ne": idem_key}}
    if signed < 0:
        query["balance"] = {"$gte": -signed}
    wallet = await db.wallets.find_one_and_update(
        query, {"$inc": {"balance": signed}, "$push": {"applied_keys": {"$each": [idem_key], "$slice": -1000}}},
        return_document=ReturnDocument.AFTER)
    if wallet is None:
        current = await db.wallets.find_one({"user_id": user_id}) or {}
        if idem_key not in (current.get("applied_keys") or []):
            await db.wallet_ledger.delete_one({"idem_key": idem_key, "status": "PENDING"})
            raise InsufficientFunds()
        wallet = current                    # already applied by the interrupted attempt
    balance = int(wallet["balance"])
    await db.wallet_ledger.update_one({"idem_key": idem_key}, {"$set": {"status": "APPLIED", "balance_after": balance}})
    entry = {**entry, "status": "APPLIED", "balance_after": balance}
    if resumed:
        raise DuplicateTransaction(entry)   # callers treat this as "already done": same as a plain retry
    return entry


async def recover_pending(min_age_seconds: int = 120) -> dict:
    """Background recovery for operations interrupted between the steps of _apply()."""
    from datetime import timedelta
    db = get_db()
    out = {"completed": 0, "dropped": 0}
    cutoff = utcnow() - timedelta(seconds=min_age_seconds)
    async for row in db.wallet_ledger.find({"status": "PENDING", "created_at": {"$lt": cutoff}}).limit(100):
        w = await db.wallets.find_one({"user_id": row["user_id"]}) or {}
        if row["idem_key"] in (w.get("applied_keys") or []):
            await db.wallet_ledger.update_one({"idem_key": row["idem_key"], "status": "PENDING"},
                                              {"$set": {"status": "APPLIED", "balance_after": int(w.get("balance", 0))}})
            out["completed"] += 1
        else:
            await db.wallet_ledger.delete_one({"idem_key": row["idem_key"], "status": "PENDING"})   # never applied
            out["dropped"] += 1
    if out["completed"] or out["dropped"]:
        log.warning("wallet recovery: %s", out)
    return out


async def credit(user_id: int, amount: int, *, idem_key: str, kind: str = "CREDIT", ref: str = "", actor: str = "system", note: str = "") -> dict:
    if amount <= 0:
        raise ValueError("credit amount must be positive")
    return await _apply(user_id, amount, kind, idem_key, ref, actor, note)


async def debit(user_id: int, amount: int, *, idem_key: str, kind: str = "DEBIT", ref: str = "", actor: str = "system", note: str = "") -> dict:
    if amount <= 0:
        raise ValueError("debit amount must be positive")
    return await _apply(user_id, -amount, kind, idem_key, ref, actor, note)


async def refund_order(user_id: int, amount: int, order_id: str, actor: str, note: str = "") -> dict:
    """One refund per order, enforced by the idem key."""
    return await credit(user_id, amount, idem_key=f"refund:{order_id}", kind="REFUND", ref=order_id, actor=actor, note=note)


async def reconcile(user_id: int) -> dict:
    db = get_db()
    total = 0
    async for row in db.wallet_ledger.find({"user_id": user_id, "status": {"$ne": "PENDING"}}):
        total += int(row["amount"])
    balance = await get_balance(user_id)
    return {"user_id": user_id, "ledger_sum": total, "balance": balance, "ok": total == balance}
