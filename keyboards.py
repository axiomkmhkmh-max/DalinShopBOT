from aiogram.types import InlineKeyboardButton as IB
from aiogram.types import InlineKeyboardMarkup, KeyboardButton, ReplyKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

import texts as T


def main_menu() -> ReplyKeyboardMarkup:
    rows = [
        [T.MENU_SHOP],
        [T.MENU_PREMIUM, T.MENU_STARS],
        [T.MENU_GIFTS, T.MENU_WALLET],
        [T.MENU_ORDERS, T.MENU_DISCOUNT],
        [T.MENU_INVITE, T.MENU_SUPPORT],
        [T.MENU_ACCOUNT],
    ]
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=t) for t in r] for r in rows], resize_keyboard=True
    )


def categories_kb(categories: list[dict]) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    for c in categories:
        b.button(text=c["name"], callback_data=f"cat:{c['public_id']}")
    b.adjust(1)
    return b.as_markup()


def products_kb(products: list[dict], back: str | None = None) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    for p in products:
        b.button(text=p["name"], callback_data=f"prod:{p['public_id']}")
    b.adjust(1)
    if back:
        b.row(IB(text=T.BACK, callback_data=back))
    return b.as_markup()


def product_kb(pid: str, buyable: bool, back: str) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    if buyable:
        for n in (1, 2, 3):
            b.button(text=f"خرید × {n}", callback_data=f"qty:{pid}:{n}")
        b.adjust(3)
    b.row(IB(text=T.BACK, callback_data=back))
    return b.as_markup()


def payment_methods_kb(pid: str, qty: int, methods: list[str]) -> InlineKeyboardMarkup:
    labels = {"stars": T.PAY_STARS, "wallet": T.PAY_WALLET, "mock": T.PAY_MOCK}
    b = InlineKeyboardBuilder()
    for m in methods:
        b.button(text=labels.get(m, m), callback_data=f"pay:{m}:{pid}:{qty}")
    b.adjust(1)
    b.row(IB(text=T.BACK, callback_data=f"prod:{pid}"))
    return b.as_markup()


def order_kb(oid: str, payable: bool) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    if payable:
        b.button(text="پرداخت (Stars)", callback_data=f"repay:{oid}")
    b.button(text=T.BACK, callback_data="orders")
    b.adjust(1)
    return b.as_markup()


def ticket_cat_kb() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    for k, v in T.TICKET_CATEGORIES.items():
        b.button(text=v, callback_data=f"tk:{k}")
    b.adjust(2)
    return b.as_markup()
