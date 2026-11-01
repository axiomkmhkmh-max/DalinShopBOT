"""Bot side of the wallet top-up (Zarinpal). Nothing here moves money: it only creates a top-up and shows the pay link.
The wallet is credited later, on the server, after Zarinpal's verify (topups.settle).

No Telegram login code / password / 2FA is ever requested anywhere in the bot."""
from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton as IB, InlineKeyboardMarkup, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

import texts as T
import topups, wallet
from util import fmt_toman, parse_int

router = Router()


class TopupStates(StatesGroup):
    amount = State()


async def topup_menu_kb() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    for amount in await topups.get_presets():
        b.button(text=fmt_toman(amount), callback_data=f"wt:a:{amount}")
    b.button(text="✏️ مبلغ دلخواه", callback_data="wt:c")
    b.adjust(2)
    return b.as_markup()


async def _start_topup(message: Message, user_id: int, amount: int) -> None:
    try:
        row = await topups.create_topup(user_id, amount)
    except topups.TopupError as exc:
        await message.answer(str(exc))
        return
    kb = InlineKeyboardMarkup(inline_keyboard=[[IB(text="💳 پرداخت امن (زرین‌پال)", url=row["pay_url"])]])
    await message.answer(f"شارژ کیف پول: {fmt_toman(amount)}\nبعد از پرداخت، کیف پول خودکار شارژ می‌شود و پیام تأیید می‌گیرید.",
                         reply_markup=kb)


@router.callback_query(F.data == "wt:chk")
async def check_mine(c: CallbackQuery):
    codes = await topups.check_user_topups(c.from_user.id)
    if not codes:
        text = "شارژ در انتظار پرداخت ندارید."
    elif "PAID" in codes:
        text = "✅ شارژ شما تأیید شد (پیام تأیید جداگانه ارسال می‌شود)."
    elif "UNAVAILABLE" in codes:
        text = "بررسی درگاه فعلاً ممکن نیست؛ چند دقیقه بعد دوباره بزنید. اگر پرداخت کرده‌اید، خودکار اعمال می‌شود."
    else:
        text = "پرداختی تأییدشده پیدا نشد. اگر پرداخت کرده‌اید چند دقیقه صبر کنید."
    await c.answer(text, show_alert=True)


@router.callback_query(F.data == "wt:menu")
async def menu(c: CallbackQuery):
    if not await topups.is_enabled():
        await c.answer("شارژ کیف پول فعال نیست.", show_alert=True)
        return
    await c.message.answer("مبلغ شارژ را انتخاب کنید:", reply_markup=await topup_menu_kb())
    await c.answer()


@router.callback_query(F.data.startswith("wt:a:"))
async def pick(c: CallbackQuery):
    amount = parse_int(c.data.split(":", 2)[2])
    if amount is None:
        await c.answer("مبلغ نامعتبر است.", show_alert=True)
        return
    await c.answer()
    await _start_topup(c.message, c.from_user.id, amount)


@router.callback_query(F.data == "wt:c")
async def custom(c: CallbackQuery, state: FSMContext):
    if not await topups.is_enabled():
        await c.answer("شارژ کیف پول فعال نیست.", show_alert=True)
        return
    lo, hi = await topups.limits()
    await state.set_state(TopupStates.amount)
    await c.message.answer(f"مبلغ شارژ را به تومان بفرستید (بین {fmt_toman(lo)} و {fmt_toman(hi)}).\nبرای انصراف: /cancel")
    await c.answer()


@router.message(Command("cancel"))
async def cancel(m: Message, state: FSMContext):
    if await state.get_state() is not None:
        await state.clear()
        await m.answer("لغو شد.")


@router.message(TopupStates.amount, F.text)
async def amount_message(m: Message, state: FSMContext):
    if m.text in T.ALL_MENU_BUTTONS:                      # the customer tapped a menu button instead of typing a number
        await state.clear()
        await m.answer("لغو شد. دوباره از منو انتخاب کنید.")
        return
    amount = parse_int(m.text)
    if amount is None:
        await m.answer("فقط یک عدد (تومان) بفرستید، یا /cancel.")
        return
    await state.clear()
    await _start_topup(m, m.from_user.id, amount)
