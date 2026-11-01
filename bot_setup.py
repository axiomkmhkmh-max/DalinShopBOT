from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.fsm.storage.memory import MemoryStorage

import handler_wallet as wallet_h, handler_account as account, handler_payments as payments, handler_shop as shop, handler_start as start, handler_support as support
from middlewares import UserMiddleware


def build_bot(token: str) -> Bot:
    return Bot(token=token, default=DefaultBotProperties(parse_mode=None))


def build_dispatcher() -> Dispatcher:
    dp = Dispatcher(storage=MemoryStorage())
    mw = UserMiddleware()
    dp.message.middleware(mw)
    dp.callback_query.middleware(mw)
    # pre_checkout_query must be answered fast and does not need the user middleware DB write,
    # but registering the user there is harmless; we intentionally do NOT attach it.
    dp.include_router(start.router)
    dp.include_router(wallet_h.router)      # first: its FSM (custom top-up amount) must win over menu handlers
    dp.include_router(payments.router)
    dp.include_router(shop.router)
    dp.include_router(account.router)
    dp.include_router(support.router)
    return dp
