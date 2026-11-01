"""Purchase flow.  Stars orders:  package/custom amount -> payment method -> destination @username -> invoice -> pay.

Payment methods
  * Telegram Stars (XTR invoice)          - official Bot API
  * internal wallet (if enabled)          - atomic debit, idempotent per order; shows a top-up button when the balance is low
Delivery is NOT decided here: see fulfillment.py / stars_delivery.py (Stars delivery to the destination account is manual
unless an official sender is registered)."""
from __future__ import annotations

import html
import logging

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (CallbackQuery, InlineKeyboardButton as IB, InlineKeyboardMarkup, LabeledPrice, Message,
                           PreCheckoutQuery)

import catalog, fulfillment, orders, topups, wallet
import texts as T
from constants import OrderStatus as S
from payment_base import ProviderDisabled
from payment_manager import get_provider
from payment_stars import StarsProvider
from payment_wallet import WalletProvider
from stars_targets import InvalidUsername, normalize_username
from util import as_aware, fa_digits, fmt_toman, utcnow

router = Router()
log = logging.getLogger(__name__)
stars: StarsProvider = get_provider("stars")  # type: ignore[assignment]


class StarsTarget(StatesGroup):
    username = State()


async def send_stars_invoice(bot, chat_id: int, order: dict) -> None:
    init = await stars.create_payment(order)
    d = init.data
    desc = d["description"]
    if order.get("target_username"):
        desc = f"{desc} - حساب مقصد: @{order['target_username']}"[:255]
    await bot.send_invoice(
        chat_id=chat_id, title=d["title"], description=desc, payload=d["payload"],
        currency=d["currency"], prices=[LabeledPrice(label=d["title"], amount=d["amount"])],
    )  # provider_token intentionally omitted: Stars (XTR) need none


def invoice_text(order: dict) -> str:
    """The invoice the customer confirms: what, how many, to whom, how much, which order."""
    lines = [f"<b>فاکتور سفارش</b>", f"شناسه سفارش: {order['public_id'][:8]}"]
    if order.get("target_username"):
        lines.append(f"تعداد Stars: {fa_digits(order['quantity'] * int((order.get('delivery_spec') or {}).get('amount') or 1))}")
        lines.append(f"حساب مقصد: @{html.escape(order['target_username'])}")
    else:
        lines.append(f"محصول: {html.escape(order['product_name'])} × {fa_digits(order['quantity'])}")
    lines.append(f"قیمت: {fmt_toman(order['total_toman'])}")
    return "\n".join(lines)


async def _fail(message: Message, text: str) -> None:
    await message.answer(text)


async def _create_and_pay(bot, message: Message, user_id: int, pid: str, qty: int, method: str,
                          target: str | None = None) -> None:
    try:
        order = await orders.create_order(user_id, pid, qty, method, target_username=target)
    except orders.OrderError as exc:
        await _fail(message, str(exc) or T.PRICE_PAUSED)
        return
    if method == "stars":
        if not await stars.is_enabled():
            await _fail(message, "این روش پرداخت فعال نیست.")
            return
        try:
            await send_stars_invoice(bot, message.chat.id, order)
        except ValueError as exc:
            await _fail(message, str(exc))
        return
    if method == "wallet":
        if not await get_provider("wallet").is_enabled():
            await _fail(message, "این روش پرداخت فعال نیست.")
            return
        bal = await wallet.get_balance(user_id)
        kb = InlineKeyboardMarkup(inline_keyboard=[[IB(text=T.PAY_WALLET_CONFIRM, callback_data=f"wpay:{order['public_id']}")]])
        await message.answer(invoice_text(order) + f"\nموجودی کیف پول: {fmt_toman(bal)}", reply_markup=kb, parse_mode="HTML")


@router.callback_query(F.data.startswith("pay:"))
async def pay(c: CallbackQuery, state: FSMContext):
    _, method, pid, n = c.data.split(":")
    method = method if method in ("stars", "wallet") else "stars"
    product = await catalog.get_product(pid)
    await c.answer()
    if product and product.get("kind") == "stars":
        # Stars always go to an account the customer names (it can be their own).
        await state.set_state(StarsTarget.username)
        await state.update_data(method=method, pid=pid, qty=int(n))
        rows = []
        if c.from_user.username:
            rows.append([IB(text=f"برای خودم (@{c.from_user.username})", callback_data="stg:me")])
        await c.message.answer(T.ASK_TARGET, reply_markup=InlineKeyboardMarkup(inline_keyboard=rows) if rows else None)
        return
    await _create_and_pay(c.bot, c.message, c.from_user.id, pid, int(n), method)


@router.callback_query(StarsTarget.username, F.data == "stg:me")
async def target_me(c: CallbackQuery, state: FSMContext):
    if not c.from_user.username:
        await c.answer("حساب شما یوزرنیم ندارد؛ یوزرنیم مقصد را تایپ کنید.", show_alert=True)
        return
    await _target_chosen(c.bot, c.message, c.from_user.id, state, c.from_user.username)
    await c.answer()


@router.message(StarsTarget.username, F.text)
async def target_message(m: Message, state: FSMContext):
    if m.text.startswith("/cancel") or m.text in T.ALL_MENU_BUTTONS:
        await state.clear()
        await m.answer("لغو شد. دوباره از منو انتخاب کنید.")
        return
    try:
        username = normalize_username(m.text)
    except InvalidUsername as exc:
        await m.answer(str(exc))
        return
    await _target_chosen(m.bot, m, m.from_user.id, state, username)


async def _target_chosen(bot, message: Message, user_id: int, state: FSMContext, username: str) -> None:
    data = await state.get_data()
    await state.clear()
    if not data.get("pid"):
        await message.answer("نشست منقضی شد؛ دوباره از منو انتخاب کنید.")
        return
    await _create_and_pay(bot, message, user_id, data["pid"], int(data["qty"]), data["method"], target=username)


@router.callback_query(F.data.startswith("wpay:"))
async def wallet_pay(c: CallbackQuery):
    order = await orders.get_order(c.data.split(":", 1)[1])
    if not order or order["user_id"] != c.from_user.id:
        await c.answer("سفارش یافت نشد.", show_alert=True)
        return
    if order["status"] != S.PENDING_PAYMENT or order.get("payment_method") != "wallet" or as_aware(order["expires_at"]) <= utcnow():
        await c.answer("این سفارش دیگر قابل پرداخت نیست؛ دوباره سفارش دهید.", show_alert=True)
        return
    provider: WalletProvider = get_provider("wallet")  # type: ignore[assignment]
    try:
        updated = await provider.pay(order)
    except ProviderDisabled:
        await c.answer("این روش پرداخت فعال نیست.", show_alert=True)
        return
    except wallet.InsufficientFunds:
        bal = await wallet.get_balance(c.from_user.id)
        missing = order["total_toman"] - bal
        rows = []
        if await topups.is_enabled():
            lo, _ = await topups.limits()
            rows.append([IB(text=f"➕ شارژ {fmt_toman(topups.suggest_amount(missing, lo))}",
                            callback_data=f"wt:a:{topups.suggest_amount(missing, lo)}")])
            rows.append([IB(text="شارژ با مبلغ دیگر", callback_data="wt:menu")])
        await c.message.answer(f"موجودی کیف پول کافی نیست.\nموجودی: {fmt_toman(bal)}\nکسری: {fmt_toman(missing)}\n"
                               "بعد از شارژ، دوباره روی «پرداخت از کیف پول» همین فاکتور بزنید (فاکتور تا پایان مهلت معتبر است).",
                               reply_markup=InlineKeyboardMarkup(inline_keyboard=rows) if rows else None)
        await c.answer()
        return
    if updated:
        await c.message.answer("✅ پرداخت انجام شد.")
        await fulfillment.on_order_paid(updated)
    else:
        await c.message.answer("این سفارش قبلاً پرداخت شده یا بسته شده است.")
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
    except ValueError:
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
