"""Telegram Stars flow: create order -> send XTR invoice -> pre_checkout -> successful_payment."""
from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.types import CallbackQuery, LabeledPrice, Message, PreCheckoutQuery

import texts as T
from payment_manager import get_provider
from payment_stars import StarsProvider
from payment_wallet import WalletProvider
import fulfillment, notifier, orders, wallet
from util import fmt_toman

router = Router()
log = logging.getLogger(__name__)
stars: StarsProvider = get_provider("stars")  # type: ignore[assignment]


async def send_stars_invoice(bot, chat_id: int, order: dict) -> None:
    init = await stars.create_payment(order)
    d = init.data
    await bot.send_invoice(
        chat_id=chat_id, title=d["title"], description=d["description"], payload=d["payload"],
        currency=d["currency"], prices=[LabeledPrice(label=d["title"], amount=d["amount"])],
    )  # provider_token intentionally omitted: Stars (XTR) need none


@router.callback_query(F.data.startswith("pay:"))
async def pay(c: CallbackQuery):
    _, method, pid, n = c.data.split(":")
    try:
        order = await orders.create_order(c.from_user.id, pid, int(n), method if method in ("stars", "wallet") else "stars")
    except orders.OrderError as exc:
        await c.answer(str(exc) or T.PRICE_PAUSED, show_alert=True)
        return
    if method == "stars":
        if not await stars.is_enabled():
            await c.answer("این روش پرداخت فعال نیست.", show_alert=True)
            return
        try:
            await send_stars_invoice(c.bot, c.message.chat.id, order)
        except ValueError as exc:
            await c.answer(str(exc), show_alert=True)
            return
        await c.answer("فاکتور ارسال شد.")
    elif method == "wallet":
        provider: WalletProvider = get_provider("wallet")  # type: ignore[assignment]
        if not await provider.is_enabled():
            await c.answer("این روش پرداخت فعال نیست.", show_alert=True)
            return
        try:
            updated = await provider.pay(order)
        except wallet.InsufficientFunds:
            await c.answer("موجودی کیف پول کافی نیست.", show_alert=True)
            return
        if updated:
            await c.message.answer("✅ پرداخت انجام شد.")
            await fulfillment.on_order_paid(updated)
        await c.answer()


@router.callback_query(F.data.startswith("repay:"))
async def repay(c: CallbackQuery):
    order = await orders.get_order(c.data.split(":", 1)[1])
    if not order or order["user_id"] != c.from_user.id:
        await c.answer("سفارش یافت نشد.", show_alert=True)
        return
    try:
        await send_stars_invoice(c.bot, c.message.chat.id, order)
        await c.answer("فاکتور ارسال شد.")
    except ValueError as exc:
        await c.answer("این سفارش دیگر قابل پرداخت نیست.", show_alert=True)


@router.pre_checkout_query()
async def pre_checkout(q: PreCheckoutQuery):
    ok, err = await stars.pre_checkout(q.from_user.id, q.invoice_payload, q.currency, q.total_amount)
    if ok:
        await q.answer(ok=True)
    else:
        await q.answer(ok=False, error_message=err)


@router.message(F.successful_payment)
async def successful_payment(m: Message):
    sp = m.successful_payment
    result = await stars.handle_successful_payment(
        m.from_user.id, sp.invoice_payload, sp.currency, sp.total_amount,
        sp.telegram_payment_charge_id, sp.provider_payment_charge_id or "",
    )
    if result.code == "OK":
        await m.answer(f"✅ پرداخت شما تأیید شد.\nشماره سفارش: {result.order['public_id'][:8]}")
        await fulfillment.on_order_paid(result.order)
    elif result.code == "DUPLICATE":
        log.info("duplicate successful_payment ignored")
    else:
        await m.answer("پرداخت شما دریافت شد اما سفارش نیاز به بررسی دارد. پشتیبانی با شما تماس می‌گیرد؛ نیازی به پرداخت مجدد نیست.")
