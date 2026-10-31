import html

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message

import keyboards as K
import texts as T
from payment_manager import available_methods
import catalog
from catalog import AUTO_DELIVERIES
from util import fa_digits, fmt_toman

router = Router()


async def _show_categories(m: Message):
    cats = await catalog.list_categories()
    if not cats:
        await m.answer(T.NO_PRODUCTS)
        return
    await m.answer("دسته‌بندی را انتخاب کنید:", reply_markup=K.categories_kb(cats))


@router.message(F.text == T.MENU_SHOP)
@router.message(Command("shop"))
@router.message(Command("products"))
async def shop(m: Message):
    await _show_categories(m)


async def _kind_list(m: Message, kind: str):
    prods = await catalog.list_products(kind=kind)
    if not prods:
        await m.answer(T.NO_PRODUCTS)
        return
    await m.answer("محصول را انتخاب کنید:", reply_markup=K.products_kb(prods))


@router.message(F.text == T.MENU_PREMIUM)
async def premium(m: Message):
    await _kind_list(m, "premium")


@router.message(F.text == T.MENU_STARS)
async def stars(m: Message):
    await _kind_list(m, "stars")


@router.message(F.text == T.MENU_GIFTS)
async def gifts(m: Message):
    await _kind_list(m, "gift")


@router.message(F.text == T.MENU_DISCOUNT)
async def discount(m: Message):
    await m.answer(T.DISCOUNT_SOON)


@router.callback_query(F.data.startswith("cat:"))
async def category(c: CallbackQuery):
    cid = c.data.split(":", 1)[1]
    prods = await catalog.list_products(category_id=cid)
    if not prods:
        await c.message.edit_text(T.NO_PRODUCTS)
    else:
        await c.message.edit_text("محصول را انتخاب کنید:", reply_markup=K.products_kb(prods))
    await c.answer()


@router.callback_query(F.data.startswith("prod:"))
async def product(c: CallbackQuery):
    pid = c.data.split(":", 1)[1]
    p = await catalog.get_product(pid)
    if not p or not p.get("enabled"):
        await c.answer("محصول یافت نشد.", show_alert=True)
        return
    ok, reason, quote = await catalog.purchasable(p)
    lines = [f"<b>{html.escape(p['name'])}</b>", html.escape(p.get("description", ""))]
    if ok:
        lines.append(f"قیمت: {fa_digits(quote['stars_amount'])} Stars")
        lines.append(f"معادل تقریبی: {fmt_toman(quote['final_toman'])}")
        lines.append(T.DELIVERY_AUTO if p.get("delivery") in AUTO_DELIVERIES else T.DELIVERY_MANUAL)
    else:
        lines.append(f"⛔️ {reason}")
    await c.message.edit_text("\n".join(x for x in lines if x), reply_markup=K.product_kb(pid, ok, "shop"), parse_mode="HTML")
    await c.answer()


@router.callback_query(F.data == "shop")
async def back_shop(c: CallbackQuery):
    cats = await catalog.list_categories()
    await c.message.edit_text("دسته‌بندی را انتخاب کنید:", reply_markup=K.categories_kb(cats))
    await c.answer()


@router.callback_query(F.data.startswith("qty:"))
async def qty(c: CallbackQuery):
    _, pid, n = c.data.split(":")
    p = await catalog.get_product(pid)
    if not p:
        await c.answer("محصول یافت نشد.", show_alert=True)
        return
    ok, reason, quote = await catalog.purchasable(p)
    if not ok:
        await c.answer(reason, show_alert=True)
        return
    qty_i = int(n)
    methods = [m.key for m in await available_methods(p)]
    if not methods:
        await c.answer("روش پرداخت فعالی وجود ندارد.", show_alert=True)
        return
    stars_total = quote["stars_amount"] * qty_i
    await c.message.edit_text(
        f"<b>{html.escape(p['name'])}</b>\nتعداد: {fa_digits(qty_i)}\nمبلغ: {fa_digits(stars_total)} Stars\n\nروش پرداخت را انتخاب کنید:",
        reply_markup=K.payment_methods_kb(pid, qty_i, methods), parse_mode="HTML")
    await c.answer()
