"""Wallet top-up through Zarinpal.

Money rules (all enforced HERE, on the server):
  * the amount that is credited is the amount stored on the top-up row at creation - never a value from the callback URL
  * the callback (?Authority=..&Status=OK) is only a hint: the wallet is credited ONLY after Zarinpal's server-side
    verify answers 100/101 for (stored amount, stored authority)
  * credit happens once: wallet ledger idem_key "topup:<id>" is unique, so a replayed/duplicated/concurrent callback,
    a reconciliation run and an admin re-check can never credit twice
  * cancelled / NOK / unverified / unknown-outcome payments never touch the balance
  * network errors while verifying are "unknown", not "failed": the row stays PENDING and background reconcile retries

TELEGRAM RULE (see README): digital goods sold inside a bot should be paid with Telegram Stars. Topping up a wallet with
an external gateway and then buying digital goods with it can get the bot limited by Telegram. This feature is therefore
OFF by default and needs an explicit switch in the dashboard + ZARINPAL_MERCHANT_ID.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import timedelta

from config import get_config
from db import get_db
import access, notifier, wallet
import settings as st
from util import fmt_toman, new_id, utcnow
from zarinpal import ZarinpalClient, ZarinpalError, ZarinpalUnavailable, get_client

log = logging.getLogger(__name__)

CREATED, PENDING, PAID, CANCELLED, FAILED, EXPIRED = "CREATED", "PENDING", "PAID", "CANCELLED", "FAILED", "EXPIRED"
STATUS_FA = {CREATED: "ایجاد شده", PENDING: "در انتظار پرداخت", PAID: "پرداخت‌شده", CANCELLED: "لغو شده",
             FAILED: "ایجاد پرداخت ناموفق", EXPIRED: "منقضی"}
RECONCILE_WINDOW = timedelta(hours=24)       # keep re-checking an unpaid PENDING row this long, then EXPIRED
MAX_OPEN_PER_USER = 5                         # open (unpaid) top-ups per user in the last 30 minutes


class TopupError(Exception):
    """Message is safe to show to the customer (Persian)."""


@dataclass
class SettleResult:
    code: str          # PAID | ALREADY_PAID | CANCELLED | NOT_PAID | UNAVAILABLE | UNKNOWN
    topup: dict | None = None


def callback_base() -> str:
    cfg = get_config()
    return (getattr(cfg, "zarinpal_callback_base_url", "") or cfg.base_url or "").rstrip("/")


async def is_enabled() -> bool:
    cfg = get_config()
    return bool(await st.get_bool("zarinpal_topup_enabled") and cfg.zarinpal_merchant_id and callback_base())


async def limits() -> tuple[int, int]:
    return await st.get_int("topup_min_toman"), await st.get_int("topup_max_toman")


async def get_presets() -> list[int]:
    raw = await st.get_setting("topup_presets") or ""
    lo, hi = await limits()
    return [int(p) for p in raw.split(",") if p.strip().isdigit() and lo <= int(p) <= hi]


def suggest_amount(missing: int, minimum: int, step: int = 1000) -> int:
    """Smallest top-up (multiple of `step`) that covers `missing` and respects the minimum."""
    need = max(int(missing), int(minimum))
    return -(-need // step) * step


async def create_topup(user_id: int, amount_toman: int, *, client: ZarinpalClient | None = None) -> dict:
    """Create the row, ask Zarinpal for an authority, return the row + pay_url. Raises TopupError."""
    if not await is_enabled():
        raise TopupError("شارژ کیف پول فعال نیست.")
    allowed, why = access.check_payment_access(user_id)
    if not allowed:
        raise TopupError(why)
    if isinstance(amount_toman, bool) or not isinstance(amount_toman, int):
        raise TopupError("مبلغ نامعتبر است.")
    lo, hi = await limits()
    if not lo <= amount_toman <= hi:
        raise TopupError(f"مبلغ شارژ باید بین {fmt_toman(lo)} و {fmt_toman(hi)} باشد.")
    db = get_db()
    now = utcnow()
    open_now = await db.topups.count_documents({"user_id": user_id, "status": {"$in": [CREATED, PENDING]},
                                                "created_at": {"$gt": now - timedelta(minutes=30)}})
    if open_now >= MAX_OPEN_PER_USER:
        raise TopupError("چند پرداخت باز دارید؛ چند دقیقه صبر کنید یا آن‌ها را تکمیل کنید.")
    try:
        client = client or get_client()
    except ZarinpalError:
        raise TopupError("درگاه پرداخت به‌درستی تنظیم نشده است.") from None
    tid = new_id()
    row = {"public_id": tid, "user_id": user_id, "amount_toman": amount_toman, "amount_rial": amount_toman * 10,
           "status": CREATED, "created_at": now, "updated_at": now,
           "history": [{"to": CREATED, "at": now, "note": "created"}]}
    await db.topups.insert_one(row)
    try:
        authority = await client.request_payment(
            row["amount_rial"], f"{callback_base()}/pay/zarinpal/callback?t={tid}", "شارژ کیف پول")
    except ZarinpalError as exc:
        log.warning("zarinpal refused to create payment (code %s) for topup %s", exc.code, tid[:8])
        await _set(tid, [CREATED], FAILED, note=f"gateway refused: {exc.code}", extra={"error": f"code {exc.code}"})
        raise TopupError("ایجاد پرداخت ناموفق بود. چند دقیقه بعد دوباره تلاش کنید.") from None
    except ZarinpalUnavailable:
        log.warning("zarinpal unreachable while creating topup %s", tid[:8])
        await _set(tid, [CREATED], FAILED, note="gateway unavailable", extra={"error": "unavailable"})
        raise TopupError("درگاه پرداخت در دسترس نیست. چند دقیقه بعد دوباره تلاش کنید.") from None
    updated = await _set(tid, [CREATED], PENDING, note="authority issued", extra={"authority": authority})
    return {**(updated or row), "pay_url": client.start_pay_url(authority)}


async def _set(public_id: str, from_states: list[str], to: str, *, note: str = "", extra: dict | None = None) -> dict | None:
    now = utcnow()
    from pymongo import ReturnDocument
    return await get_db().topups.find_one_and_update(
        {"public_id": public_id, "status": {"$in": from_states}},
        {"$set": {"status": to, "updated_at": now, **(extra or {})}, "$push": {"history": {"to": to, "at": now, "note": note}}},
        return_document=ReturnDocument.AFTER)


async def get_topup(public_id: str) -> dict | None:
    return await get_db().topups.find_one({"public_id": public_id})


async def settle(public_id: str, authority: str, status_param: str | None, *, client: ZarinpalClient | None = None,
                 from_callback: bool = True) -> SettleResult:
    """Decide what a callback / reconcile run means. The ONLY place that credits the wallet for a top-up."""
    db = get_db()
    t = await get_topup(public_id)
    if not t or not authority or t.get("authority") != authority:
        return SettleResult("UNKNOWN", None)               # forged / mismatching callback: change nothing
    if t["status"] == PAID:
        return SettleResult("ALREADY_PAID", t)
    if from_callback and status_param != "OK":
        await _set(public_id, [CREATED, PENDING], CANCELLED, note="user cancelled / gateway reported NOK")
        return SettleResult("CANCELLED", await get_topup(public_id))
    try:
        client = client or get_client()
        res = await client.verify(int(t["amount_rial"]), authority)
    except (ZarinpalUnavailable, ZarinpalError) as exc:
        # UNKNOWN outcome (timeout / network / gateway error): remember it, keep PENDING, reconcile + manual recheck retry
        log.warning("zarinpal verify outcome unknown for topup %s (%s)", public_id[:8], type(exc).__name__)
        await db.topups.update_one({"public_id": public_id}, {
            "$set": {"last_verify_code": "unknown", "last_unknown_at": utcnow(), "last_verify_at": utcnow()},
            "$inc": {"unknown_count": 1}})
        return SettleResult("UNAVAILABLE", await get_topup(public_id))
    await db.topups.update_one({"public_id": public_id},
                               {"$set": {"last_verify_code": res.code, "last_verify_at": utcnow()}})
    if not res.paid:
        log.info("topup %s not paid according to zarinpal (code %s)", public_id[:8], res.code)
        return SettleResult("NOT_PAID", await get_topup(public_id))
    try:
        await wallet.credit(t["user_id"], int(t["amount_toman"]), idem_key=f"topup:{public_id}", kind="TOPUP",
                            ref=public_id, actor="zarinpal", note=f"zarinpal ref_id={res.ref_id}")
    except wallet.DuplicateTransaction:
        pass                                               # already credited (replay / race): fine
    flipped = await _set(public_id, [CREATED, PENDING, CANCELLED, EXPIRED, FAILED], PAID, note=f"verified code={res.code}",
                         extra={"ref_id": res.ref_id, "card_pan": res.card_pan, "verified_at": utcnow()})
    if flipped is None:
        return SettleResult("ALREADY_PAID", await get_topup(public_id))
    bal = await wallet.get_balance(t["user_id"])
    await notifier.send_user(t["user_id"], f"✅ کیف پول شما {fmt_toman(t['amount_toman'])} شارژ شد.\n"
                                           f"موجودی فعلی: {fmt_toman(bal)}\nشماره پیگیری: {res.ref_id or '—'}")
    return SettleResult("PAID", flipped)


async def reconcile_pending(client: ZarinpalClient | None = None, limit: int = 50) -> dict:
    """Background: customers who paid but never came back, and rows that can no longer be paid."""
    db = get_db()
    now = utcnow()
    out = {"checked": 0, "paid": 0, "expired": 0}
    async for t in db.topups.find({"status": {"$in": [CREATED, PENDING]}, "created_at": {"$lt": now - RECONCILE_WINDOW}}).limit(limit):
        if await _set(t["public_id"], [CREATED, PENDING], EXPIRED, note="not paid within the window"):
            out["expired"] += 1
    query = {"status": PENDING, "created_at": {"$lt": now - timedelta(minutes=2), "$gt": now - RECONCILE_WINDOW},
             "$or": [{"last_reconcile_at": {"$exists": False}}, {"last_reconcile_at": {"$lt": now - timedelta(minutes=10)}}]}
    async for t in db.topups.find(query).limit(limit):
        await db.topups.update_one({"public_id": t["public_id"]}, {"$set": {"last_reconcile_at": now}})
        out["checked"] += 1
        res = await settle(t["public_id"], t["authority"], "OK", client=client, from_callback=False)
        if res.code == "PAID":
            out["paid"] += 1
    return out


async def check_user_topups(user_id: int, client: ZarinpalClient | None = None) -> list[str]:
    """Customer taps "check my top-ups": re-verify their own open top-ups of the last 24h with Zarinpal (server-side)."""
    out: list[str] = []
    since = utcnow() - RECONCILE_WINDOW
    async for t in get_db().topups.find({"user_id": user_id, "status": PENDING, "created_at": {"$gt": since}}).limit(10):
        out.append((await settle(t["public_id"], t["authority"], "OK", client=client, from_callback=False)).code)
    return out
