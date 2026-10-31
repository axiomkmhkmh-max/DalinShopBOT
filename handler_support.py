from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message

import keyboards as K
import texts as T
from db import get_db
import notifier
from util import new_id, utcnow

router = Router()


class Ticket(StatesGroup):
    waiting_text = State()


@router.message(F.text == T.MENU_SUPPORT)
@router.message(Command("support"))
async def support(m: Message, state: FSMContext):
    await state.clear()
    await m.answer("موضوع پیام را انتخاب کنید:", reply_markup=K.ticket_cat_kb())


@router.callback_query(F.data.startswith("tk:"))
async def pick(c: CallbackQuery, state: FSMContext):
    await state.set_state(Ticket.waiting_text)
    await state.update_data(cat=c.data.split(":", 1)[1])
    await c.message.edit_text("پیام خود را بنویسید (می‌توانید عکس یا فایل هم بفرستید):")
    await c.answer()


@router.message(Ticket.waiting_text)
async def receive(m: Message, state: FSMContext):
    data = await state.get_data()
    text = m.text or m.caption or "[فایل]"
    file_id = None
    if m.photo:
        file_id = m.photo[-1].file_id
    elif m.document:
        file_id = m.document.file_id
    await get_db().tickets.insert_one({
        "public_id": new_id(), "user_id": m.from_user.id, "category": data.get("cat", "other"),
        "status": "OPEN", "created_at": utcnow(),
        "messages": [{"from": "user", "text": text, "file_id": file_id, "at": utcnow()}],
    })
    await state.clear()
    await m.answer("پیام شما ثبت شد. پاسخ از همین ربات ارسال می‌شود.", reply_markup=K.main_menu())
    await notifier.send_admins("تیکت پشتیبانی جدید ثبت شد.")
