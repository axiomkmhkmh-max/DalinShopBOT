import html

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message

import keyboards as K
import texts as T
from constants import STATUS_FA, OrderStatus as S
import orders, wallet
from util import fa_digits, fmt_toman

router = Router()


@router.message(F.text == T.MENU_ORDERS)
@router.message(Command("orders"))
async def my_orders(m: Message):
    items = await orders.list_user_orders(m.from_user.id)
    if not items:
        await m.answer("هنوز سفارشی ثبت نکرده‌اید.")
        return
    from aiogram.utils.keyboard import InlineKeyboardBuilder
    b = InlineKeyboardBuilder()
    for o in items:
        b.button(text=f"{o['product_name']} - {STATUS_FA.get(o['status'], o['status'])}", callback_data=f"ord:{o['public_id']}")
    b.adjust(1)
    await m.answer("سفارش‌های اخیر:", reply_markup=b.as_markup())


@router.callback_query(F.data == "orders")
async def back_orders(c: CallbackQuery):
    items = await orders.list_user_orders(c.from_user.id)
    from aiogram.utils.keyboard import InlineKeyboardBuilder
    b = InlineKeyboardBuilder()
    for o in items:
        b.button(text=f"{o['product_name']} - {STATUS_FA.get(o['status'], o['status'])}", callback_data=f"ord:{o['public_id']}")
    b.adjust(1)
    await c.message.edit_text("سفارش‌های اخیر:", reply_markup=b.as_markup())
    await c.answer()


@router.callback_query(F.data.startswith("ord:"))
async def order_detail(c: CallbackQuery):
    o = await orders.get_order(c.data.split(":", 1)[1])
    if not o or o["user_id"] != c.from_user.id:
        await c.answer("سفارش یافت نشد.", show_alert=True)
        return
    text = (f"<b>{html.escape(o['product_name'])}</b>\nشماره: {o['public_id'][:8]}\nتعداد: {fa_digits(o['quantity'])}\n"
            f"مبلغ: {fmt_toman(o['total_toman'])}\nوضعیت: {STATUS_FA.get(o['status'], o['status'])}")
    await c.message.edit_text(text, reply_markup=K.order_kb(o["public_id"], o["status"] == S.PENDING_PAYMENT), parse_mode="HTML")
    await c.answer()


@router.message(F.text == T.MENU_WALLET)
@router.message(Command("wallet"))
async def wallet_view(m: Message):
    bal = await wallet.get_balance(m.from_user.id)
    await m.answer(f"موجودی کیف پول: {fmt_toman(bal)}")


@router.message(F.text == T.MENU_INVITE)
async def invite(m: Message, db_user: dict):
    me = await m.bot.get_me()
    await m.answer(f"لینک دعوت شما:\nhttps://t.me/{me.username}?start=ref_{db_user['ref_code']}")


@router.message(F.text == T.MENU_ACCOUNT)
async def account(m: Message, db_user: dict):
    await m.answer(f"شناسه: {fa_digits(m.from_user.id)}\nتاریخ عضویت: {db_user['created_at']:%Y-%m-%d}")
