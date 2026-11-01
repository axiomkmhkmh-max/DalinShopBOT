"""Stars orders delivered through a SUPPLIER API: submit once, track until the supplier gives a final answer.

Two different state machines, never mixed:
  * OUR order        orders.status (+ fulfillment_status SUPPLIER_PENDING / MANUAL_REVIEW)   -> constants.canonical_status
  * the SUPPLIER side supplier_orders.track_state (+ supplier_status = the supplier's own word, kept verbatim)

track_state:  TO_SUBMIT  intent recorded, supplier has NOT confirmed an order (also after a timeout: outcome unknown)
              SUBMITTED  supplier gave us an order id; we poll it
              DELIVERED / FAILED_DEFINITE   final answers from the supplier
              MANUAL_REVIEW  a human must look (stuck, auth problem, price above sale price, unknown answers, retries used up)
              CLOSED     order was settled another way (manual delivery / refund); no more polling

Money & safety rules
  * the idempotency key is fixed per order ("shop-<order id>"): every re-submit after a timeout is replay-safe, so the
    supplier can never create two orders for one shop order
  * a timeout / 5xx / network error is NEVER a failure: no refund, the row stays TO_SUBMIT and is retried; only after
    STARS_SUPPLIER_MAX_RETRIES consecutive unknown outcomes it goes to MANUAL_REVIEW (still no refund)
  * refund happens ONLY on a definite answer (supplier rejected the order or reports cancelled/refunded/failed), once,
    through the wallet ledger (idem key "refund:<order id>")
  * several workers / processes: each row is claimed with an atomic lock (lock_until)
"""
from __future__ import annotations

import asyncio
import logging
from datetime import timedelta

from config import get_config
from constants import OrderStatus as S
from db import get_db
import notifier, orders
import stars_supplier as sup
from stars_supplier import NormalizedStatus as N, SupplierAuthError, SupplierRejected, SupplierTransient
from supplier_base import Supplier, SupplierRequest, SupplierResult
from util import as_aware, redact, utcnow

log = logging.getLogger(__name__)

POLL_PAUSE = 0.3          # seconds between supplier calls (the documented limit is 60 requests/minute per key)
LOCK_SECONDS = 90
UNKNOWN_STATUS_LIMIT = 5  # consecutive answers with a status word we do not know -> manual review
FAR = timedelta(days=3650)

TO_SUBMIT, SUBMITTED, DELIVERED, FAILED_DEFINITE, MANUAL_REVIEW, CLOSED = (
    "TO_SUBMIT", "SUBMITTED", "DELIVERED", "FAILED_DEFINITE", "MANUAL_REVIEW", "CLOSED")
TRACK_FA = {TO_SUBMIT: "در انتظار ارسال/تأیید", SUBMITTED: "ثبت‌شده نزد تأمین‌کننده", DELIVERED: "تحویل‌شده",
            FAILED_DEFINITE: "شکست قطعی", MANUAL_REVIEW: "بررسی دستی", CLOSED: "بسته (خارج از تأمین‌کننده)"}


def _cfg():
    return get_config()


def idem_key(order_id: str, rotation: int = 0) -> str:
    return f"shop-{order_id}" if rotation == 0 else f"shop-{order_id}-r{rotation}"


def _clip(text) -> str:
    return redact(str(text))[:300]


# ------------------------------------------------------------------------------------------------ guards before payment
async def guard_new_order(stars: int, sale_toman: int) -> str | None:
    """Message (Persian) when a NEW Stars order must not be accepted right now, else None.
    Checks supplier limits, that the supplier answers, and that its price is not above our sale price (no sale at a loss)."""
    if not sup.is_enabled():
        return None
    try:
        adapter = sup.get_adapter()
        if not adapter.min_stars <= stars <= adapter.max_stars:
            return f"تعداد Stars باید بین {adapter.min_stars:,} و {adapter.max_stars:,} باشد."
        quote = await adapter.quote(stars)
    except SupplierRejected:
        return "تأمین‌کننده این تعداد را نمی‌پذیرد."
    except (SupplierAuthError, SupplierTransient):
        log.warning("supplier unavailable while checking a new order")
        return "تأمین Stars موقتاً در دسترس نیست؛ کمی بعد دوباره تلاش کنید."
    if quote.amount_toman > sale_toman:
        log.warning("supplier price %s is above sale price %s", quote.amount_toman, sale_toman)
        return "قیمت این سفارش موقتاً در حال به‌روزرسانی است؛ کمی بعد دوباره تلاش کنید."
    return None


# ------------------------------------------------------------------------------------------------ row helpers
async def _get(order_id: str) -> dict | None:
    return await get_db().supplier_orders.find_one({"order_id": order_id})


async def _claim(order_id: str) -> dict | None:
    now = utcnow()
    from pymongo import ReturnDocument
    return await get_db().supplier_orders.find_one_and_update(
        {"order_id": order_id, "$or": [{"lock_until": {"$exists": False}}, {"lock_until": {"$lt": now}}]},
        {"$set": {"lock_until": now + timedelta(seconds=LOCK_SECONDS)}}, return_document=ReturnDocument.AFTER)


async def _release(order_id: str, **fields) -> None:
    await get_db().supplier_orders.update_one({"order_id": order_id}, {"$set": {"lock_until": utcnow() - timedelta(seconds=1),
                                                                                 "updated_at": utcnow(), **fields}})


def _next(seconds: float):
    return utcnow() + timedelta(seconds=max(1.0, seconds))


def _backoff(failures: int) -> float:
    base = max(5, int(_cfg().stars_supplier_poll_interval))
    return min(900.0, base * (2 ** max(0, failures - 1)))


async def _event(order_id: str, text: str) -> None:
    await get_db().supplier_orders.update_one({"order_id": order_id}, {"$push": {"events": {"at": utcnow(), "text": _clip(text)}}})


# ------------------------------------------------------------------------------------------------ entry from fulfillment
class StarsSupplierProvider(Supplier):
    key = "stars_supplier"

    async def place_order(self, req: SupplierRequest) -> SupplierResult:
        return await begin(req)


async def begin(req: SupplierRequest) -> SupplierResult:
    from stars_delivery import stars_total
    db = get_db()
    total = stars_total(req.product, req.quantity)
    if not req.target_username:
        return SupplierResult("MANUAL_REVIEW", "سفارش حساب مقصد ندارد؛ بدون یوزرنیم به تأمین‌کننده ارسال نمی‌شود.")
    try:
        adapter = sup.get_adapter()
    except SupplierAuthError as exc:
        return SupplierResult("MANUAL_REVIEW", _clip(f"تنظیمات تأمین‌کننده: {exc}"))
    if not adapter.min_stars <= total <= adapter.max_stars:
        return SupplierResult("MANUAL_REVIEW", f"تعداد {total} خارج از محدوده تأمین‌کننده است.")
    row = await _get(req.order_id)
    state = (row or {}).get("track_state")
    if state in (SUBMITTED, MANUAL_REVIEW) and (row or {}).get("external_ref"):
        return SupplierResult("SUPPLIER_PENDING", "سفارش قبلاً نزد تأمین‌کننده ثبت شده؛ فقط پیگیری می‌شود.", external_ref=row["external_ref"])
    if state == DELIVERED:
        return SupplierResult("DELIVERED", "supplier already confirmed", external_ref=(row or {}).get("external_ref", ""))
    now = utcnow()
    if row is None:
        try:
            await db.supplier_orders.insert_one({
                "order_id": req.order_id, "supplier_key": adapter.name, "track_state": TO_SUBMIT, "track_total_stars": total,
                "track_recipient": req.target_username, "track_rotation": 0, "track_idem_key": idem_key(req.order_id),
                "submit_attempts": 0, "fail_count": 0, "poll_count": 0, "unknown_status_count": 0, "next_poll_at": now,
                "created_at": now, "updated_at": now})
        except Exception:                                    # duplicate key: another call created it first
            pass
    elif state in (FAILED_DEFINITE, CLOSED, MANUAL_REVIEW):
        # admin retry after a DEFINITE failure: a fresh submission needs a fresh key (the old key replays the old answer)
        rotation = int(row.get("track_rotation") or 0) + (1 if state == FAILED_DEFINITE else 0)
        await db.supplier_orders.update_one({"order_id": req.order_id}, {"$set": {
            "track_state": TO_SUBMIT, "track_rotation": rotation, "track_idem_key": idem_key(req.order_id, rotation),
            "track_total_stars": total, "track_recipient": req.target_username, "submit_attempts": 0, "fail_count": 0,
            "next_poll_at": now, "updated_at": now}})
    locked = await _claim(req.order_id)
    if locked is None:
        return SupplierResult("SUPPLIER_PENDING", "ارسال در حال انجام است.")
    return await _submit(locked)


# ------------------------------------------------------------------------------------------------ submit
async def _submit(row: dict) -> SupplierResult:
    """Row is locked by the caller. Never raises."""
    order_id = row["order_id"]
    limit = max(1, int(_cfg().stars_supplier_max_retries))
    try:
        adapter = sup.get_adapter()
        if int(row.get("submit_attempts") or 0) == 0:
            order = await orders.get_order(order_id)
            quote = await adapter.quote(int(row["track_total_stars"]))
            if order and quote.amount_toman > int(order["total_toman"]):
                msg = f"قیمت تأمین‌کننده ({quote.amount_toman:,}) از قیمت فروش ({int(order['total_toman']):,}) بیشتر است."
                await _release(order_id, track_state=MANUAL_REVIEW, last_error=msg, next_poll_at=utcnow() + FAR)
                return SupplierResult("MANUAL_REVIEW", msg)
        await get_db().supplier_orders.update_one({"order_id": order_id}, {"$inc": {"submit_attempts": 1}})
        info = await adapter.create_order(idempotency_key=row["track_idem_key"], quantity=int(row["track_total_stars"]),
                                          recipient=row["track_recipient"], metadata={"shop_order_id": order_id})
    except SupplierRejected as exc:
        msg = _clip(f"{exc.code}: {exc}")
        await _release(order_id, track_state=FAILED_DEFINITE, last_error=msg, next_poll_at=utcnow() + FAR)
        await _event(order_id, f"rejected: {msg}")
        return SupplierResult("FAILED", msg, refund=True)                # nothing was created at the supplier
    except SupplierAuthError as exc:
        msg = _clip(f"مشکل کلید/کیف پول تأمین‌کننده ({exc.code}): {exc}")
        await _release(order_id, track_state=MANUAL_REVIEW, last_error=msg, next_poll_at=utcnow() + FAR)
        await _event(order_id, msg)
        return SupplierResult("MANUAL_REVIEW", msg)
    except Exception as exc:                                              # SupplierTransient or anything unexpected = UNKNOWN
        fails = int(row.get("fail_count") or 0) + 1
        msg = _clip(f"{type(exc).__name__}: {exc}")
        log.warning("supplier submit outcome unknown for %s (%d/%d): %s", order_id[:8], fails, limit, type(exc).__name__)
        if fails >= limit:
            await _release(order_id, track_state=MANUAL_REVIEW, fail_count=fails, last_error=msg, next_poll_at=utcnow() + FAR)
            return SupplierResult("MANUAL_REVIEW", f"نتیجه ارسال به تأمین‌کننده بعد از {fails} تلاش نامشخص ماند: {msg}")
        await _release(order_id, track_state=TO_SUBMIT, fail_count=fails, last_error=msg, next_poll_at=_next(_backoff(fails)))
        return SupplierResult("SUPPLIER_PENDING", "نتیجه ارسال نامشخص است؛ با همان شناسه دوباره بررسی می‌شود.")
    return await _record(order_id, info, submitted=True)


async def _record(order_id: str, info: sup.SupplierOrderInfo, *, submitted: bool = False) -> SupplierResult:
    interval = max(1, int(_cfg().stars_supplier_poll_interval))
    base = {"external_ref": info.ref, "supplier_status": info.raw_status, "fail_count": 0, "last_error": ""}
    if info.status is N.DELIVERED:
        await _release(order_id, track_state=DELIVERED, status="DELIVERED", next_poll_at=utcnow() + FAR, **base)
        return SupplierResult("DELIVERED", "supplier confirmed delivery", external_ref=info.ref)
    if info.status is N.FAILED:
        await _release(order_id, track_state=FAILED_DEFINITE, next_poll_at=utcnow() + FAR,
                       last_error=_clip(info.failure_reason or info.raw_status), **{k: v for k, v in base.items() if k != "last_error"})
        return SupplierResult("FAILED", _clip(f"تأمین‌کننده: {info.raw_status} {info.failure_reason}"), external_ref=info.ref, refund=True)
    extra = {"submitted_at": utcnow()} if submitted else {}
    unknown = info.status is N.UNKNOWN
    current = await _get(order_id) or {}
    keep = MANUAL_REVIEW if current.get("track_state") == MANUAL_REVIEW else SUBMITTED   # polling never hides a review flag
    await _release(order_id, track_state=keep, next_poll_at=_next(interval), **base, **extra)
    if unknown:
        await get_db().supplier_orders.update_one({"order_id": order_id}, {"$inc": {"unknown_status_count": 1}})
    else:
        await get_db().supplier_orders.update_one({"order_id": order_id}, {"$set": {"unknown_status_count": 0}})
    return SupplierResult("SUPPLIER_PENDING", f"supplier status: {info.raw_status}", external_ref=info.ref)


# ------------------------------------------------------------------------------------------------ effects on OUR order
async def _apply(order_id: str, res: SupplierResult) -> str:
    import fulfillment                                # lazy: fulfillment imports this module through supplier_base
    if res.status == "DELIVERED":
        order = await orders.get_order(order_id)
        if order and order["status"] == S.PROCESSING:
            await fulfillment._finish(order, "")
        elif order and order["status"] in (S.REFUNDED, S.REFUND_PENDING, S.CANCELLED):
            await notifier.send_admins(f"⚠️ تأمین‌کننده تحویل سفارش {order_id} را تأیید کرد، ولی سفارش قبلاً بازپرداخت/لغو شده است؛ "
                                       "بررسی مالی لازم است.")
        return "DELIVERED"
    if res.status == "FAILED":
        await fulfillment.finish_failed(order_id, res.message, refund=res.refund)
        return "FAILED"
    if res.status == "MANUAL_REVIEW":
        await fulfillment.mark_manual_review(order_id, res.message)
        return "MANUAL_REVIEW"
    return "PENDING"


async def _to_review(order_id: str, reason: str) -> str:
    await _release(order_id, track_state=MANUAL_REVIEW, last_error=_clip(reason))
    await _event(order_id, reason)
    return await _apply(order_id, SupplierResult("MANUAL_REVIEW", _clip(reason)))


# ------------------------------------------------------------------------------------------------ poll
async def _poll(row: dict) -> str:
    """Row is locked by the caller."""
    order_id = row["order_id"]
    limit = max(1, int(_cfg().stars_supplier_max_retries))
    order = await orders.get_order(order_id)
    ref = row.get("external_ref")
    if not order or not ref:
        await _release(order_id, track_state=MANUAL_REVIEW, next_poll_at=utcnow() + FAR)
        return "MANUAL_REVIEW"
    if order["status"] != S.PROCESSING:
        # settled another way (manual delivery / refund). One last look: if the supplier DID deliver after a refund, shout.
        if order["status"] in (S.REFUNDED, S.REFUND_PENDING, S.FAILED, S.CANCELLED):
            try:
                info = await sup.get_adapter().get_order(ref)
                if info.status is N.DELIVERED:
                    await notifier.send_admins(f"⚠️ سفارش {order_id} بازپرداخت/ناموفق ثبت شده ولی تأمین‌کننده آن را تحویل‌شده اعلام می‌کند. "
                                               "فوراً بررسی کنید.")
            except Exception:
                pass
        await _release(order_id, track_state=CLOSED, next_poll_at=utcnow() + FAR)
        return "CLOSED"
    try:
        info = await sup.get_adapter().get_order(ref)
    except SupplierRejected as exc:
        return await _to_review(order_id, f"تأمین‌کننده سفارش را نمی‌شناسد ({exc.code})")
    except Exception as exc:                          # transient / auth / unexpected: unknown, never a failure
        fails = int(row.get("fail_count") or 0) + 1
        log.warning("supplier poll failed for %s (%d/%d): %s", order_id[:8], fails, limit, type(exc).__name__)
        if fails >= limit and row.get("track_state") != MANUAL_REVIEW:
            await _release(order_id, fail_count=fails)
            return await _to_review(order_id, f"پیگیری وضعیت بعد از {fails} تلاش ناموفق: {type(exc).__name__}")
        await _release(order_id, fail_count=fails, last_error=_clip(f"{type(exc).__name__}: {exc}"), next_poll_at=_next(_backoff(fails)))
        return "ERROR"
    await get_db().supplier_orders.update_one({"order_id": order_id}, {"$inc": {"poll_count": 1}})
    res = await _record(order_id, info)
    outcome = await _apply(order_id, res)
    if outcome != "PENDING":
        return outcome
    fresh = await _get(order_id) or {}
    if int(fresh.get("unknown_status_count") or 0) >= UNKNOWN_STATUS_LIMIT and fresh.get("track_state") != MANUAL_REVIEW:
        return await _to_review(order_id, f"وضعیت ناشناخته از تأمین‌کننده: {info.raw_status!r}")
    started = as_aware(fresh.get("submitted_at"))
    stuck_after = timedelta(minutes=max(1, int(_cfg().stars_supplier_stuck_minutes)))
    if started and utcnow() - started > stuck_after and fresh.get("track_state") == SUBMITTED:
        return await _to_review(order_id, f"سفارش بیش از {int(stuck_after.total_seconds() // 60)} دقیقه نزد تأمین‌کننده نهایی نشده است "
                                          f"(وضعیت: {info.raw_status})")
    if fresh.get("track_state") == MANUAL_REVIEW:     # a reviewed order with a ref keeps being polled, but slowly
        await _release(order_id, next_poll_at=_next(10 * max(1, int(_cfg().stars_supplier_poll_interval))))
    return outcome if outcome != "PENDING" else "PENDING"


async def _advance(row: dict) -> str:
    if row.get("track_state") == TO_SUBMIT:
        res = await _submit(row)
        return await _apply(row["order_id"], res)
    return await _poll(row)


# ------------------------------------------------------------------------------------------------ worker
async def _claim_due() -> dict | None:
    now = utcnow()
    from pymongo import ReturnDocument
    return await get_db().supplier_orders.find_one_and_update(
        {"track_state": {"$in": [TO_SUBMIT, SUBMITTED, MANUAL_REVIEW]}, "next_poll_at": {"$lte": now},
         "$or": [{"lock_until": {"$exists": False}}, {"lock_until": {"$lt": now}}]},
        {"$set": {"lock_until": now + timedelta(seconds=LOCK_SECONDS)}}, sort=[("next_poll_at", 1)],
        return_document=ReturnDocument.AFTER)


async def run_poll_cycle(limit: int = 25) -> dict:
    """One worker pass. Safe to call from several processes at once (rows are claimed atomically)."""
    out: dict[str, int] = {}
    if not sup.is_enabled():
        return out
    for _ in range(limit):
        row = await _claim_due()
        if row is None:
            break
        try:
            result = await _advance(row)
        except Exception:
            log.exception("supplier tracking failed for %s", str(row.get("order_id"))[:8])
            await _release(row["order_id"], next_poll_at=_next(_backoff(int(row.get("fail_count") or 0) + 1)))
            result = "CRASH"
        out[result] = out.get(result, 0) + 1
        if POLL_PAUSE:
            await asyncio.sleep(POLL_PAUSE)
    return out


# ------------------------------------------------------------------------------------------------ admin actions
async def poll_now(order_id: str) -> str:
    """Admin: check this order right now."""
    row = await _claim(order_id)
    if row is None:
        return "BUSY"
    if row.get("track_state") not in (TO_SUBMIT, SUBMITTED, MANUAL_REVIEW):
        await _release(order_id)
        return "NOT_TRACKED"
    return await _advance(row)


async def retry_submit(order_id: str) -> bool:
    """Admin: a MANUAL_REVIEW order that has NO supplier id goes back to TO_SUBMIT (same idempotency key = replay-safe)."""
    res = await get_db().supplier_orders.update_one(
        {"order_id": order_id, "track_state": MANUAL_REVIEW, "external_ref": {"$in": [None, ""]}},
        {"$set": {"track_state": TO_SUBMIT, "fail_count": 0, "submit_attempts": 0, "next_poll_at": utcnow(), "lock_until": utcnow()}})
    return res.modified_count == 1
