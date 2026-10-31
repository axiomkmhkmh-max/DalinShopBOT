from aiogram import Router
from aiogram.filters import Command, CommandStart
from aiogram.types import Message

import keyboards as K
import texts as T

router = Router()


@router.message(CommandStart())
async def start(m: Message):
    await m.answer(T.WELCOME, reply_markup=K.main_menu())


@router.message(Command("help"))
async def help_(m: Message):
    await m.answer(T.HELP, reply_markup=K.main_menu())
