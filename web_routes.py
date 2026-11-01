from __future__ import annotations

import re
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from config import get_config
from constants import STATUS_FA, OrderStatus as S, canonical_status, has_permission
from db import get_db
from money import MoneyError, to_decimal, validate_profit
from payment_manager import get_provider
import background_hooks as hooks
import catalog, fulfillment, orders, stars_delivery, telegram_supply, topups, wallet
import pricing_engine as engine
import settings as st
from audit import audit
from catalog import AUTO_DELIVERIES, DELIVERY_FA
from notifier import get_bot, send_user
from telegram_supply import PREMIUM_STARS
from util import as_aware, new_id, redact, utcnow
import auth

router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).parent))
templates.env.globals["has_permission"] = has_permission

NAV = [  # (label, path, permission)
    ("نمای کلی", "/", None), ("محصولات", "/products", "products"), ("دسته‌بندی‌ها", "/categories", "products"),
    ("قیمت‌گذاری", "/pricing", "pricing_view"), ("سفارش‌ها", "/orders", "orders_view"),
    ("پرداخت‌ها", "/payments", "payments"), ("شارژ و تنظیمات کیف پول", "/topups", "payments"),
    ("کیف پول‌ها", "/wallets", "wallets_adjust"),
    ("کاربران", "/users", "users"), ("تأمین و تحویل", "/suppliers", "suppliers"),
    ("پشتیبانی", "/tickets", "support"), ("خطاها", "/errors", "orders_view"), ("لاگ ممیزی", "/audit", "audit"),
]


def render(request: Request, admin: dict, template: str, **ctx):
    nav = [n for n in NAV if n[2] is None or has_permission(admin["role"], n[2])
           or (n[2] == "pricing_view" and has_permission(admin["role"], "pricing"))
           or (n[2] == "orders_view" and has_permission(admin["role"], "orders"))]
    flash = request.session.pop("flash", None)
    return templates.TemplateResponse(request, template, {
        "admin": admin, "nav": nav, "csrf": auth.csrf_token(request), "flash": flash, "path": request.url.path, **ctx})


def back(request: Request, path: str, msg: str | None = None):
    if msg:
        request.session["flash"] = msg
    return RedirectResponse(path, status_code=303)


def can(admin: dict, *perms: str) -> bool:
    return any(has_permission(admin["role"], p) for p in perms)


def need_any(admin: dict, *perms: str) -> None:
    if not can(admin, *perms):
        from fastapi import HTTPException
        raise HTTPException(403, "دسترسی کافی ندارید")


def form_field(name, label, value="", type_="text", options=None, required=False):
    return {"name": name, "label": label, "value": value, "type": type_, "options": options or [], "required": required}


# ---------------- auth ----------------
@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    return templates.TemplateResponse(request, "login.html", {"csrf": auth.csrf_token(request), "error": None})


@router.post("/login")
async def login(request: Request, username: str = Form(...), password: str = Form(...), csrf: str = Form("")):
    auth.check_csrf(request, csrf)
    ip = request.client.host if request.client else "?"
    if not auth.login_rate_ok(ip):
        return templates.TemplateResponse(request, "login.html", {"csrf": auth.csrf_token(request), "error": "تلاش بیش از حد. بعداً دوباره امتحان کنید."}, status_code=429)
    admin = await get_db().admin_users.find_one({"username": username, "active": True})
    if not admin or not auth.verify_password(password, admin["password_hash"]):
        auth.login_failed(ip)
        await audit(username[:40], "login_failed", ip)
        return templates.TemplateResponse(request, "login.html", {"csrf": auth.csrf_token(request), "error": "نام کاربری یا رمز اشتباه است."}, status_code=401)
    request.session.clear()
    request.session["admin"] = admin["username"]
    await audit(username, "login", ip)
    return RedirectResponse("/", status_code=303)


@router.post("/logout")
async def logout(request: Request, csrf: str = Form("")):
    auth.check_csrf(request, csrf)
    request.session.clear()
    return RedirectResponse("/login", status_code=303)


# ---------------- overview ----------------
@router.get("/", response_class=HTMLResponse)
async def overview(request: Request, admin=Depends(auth.current_admin)):
    db = get_db()
    since = utcnow() - timedelta(days=14)
    pipeline = [
        {"$match": {"created_at": {"$gte": since}, "status": {"$in": [S.PAID, S.PROCESSING, S.FULFILLED]}}},
        {"$group": {"_id": {"$dateToString": {"format": "%Y-%m-%d", "date": "$created_at"}},
                    "revenue": {"$sum": "$total_toman"}, "count": {"$sum": 1}}},
        {"$sort": {"_id": 1}},
    ]
    daily = [d async for d in db.orders.aggregate(pipeline)]
    status_counts = {s: await db.orders.count_documents({"status": s}) for s in STATUS_FA}
    revenue_total = 0
    async for d in db.orders.aggregate([{"$match": {"status": {"$in": [S.PAID, S.PROCESSING, S.FULFILLED]}}},
                                        {"$group": {"_id": None, "t": {"$sum": "$total_toman"}}}]):
        revenue_total = d["t"]
    wallet_total = 0
    async for d in db.wallets.aggregate([{"$group": {"_id": None, "t": {"$sum": "$balance"}}}]):
        wallet_total = d["t"]
    sup = await telegram_supply.get_supply_status()
    manual_pending = await db.orders.count_documents({"status": S.PROCESSING, "fulfillment_status": "MANUAL_PENDING"})
    stats = [
        ("درآمد کل (تومان)", f"{revenue_total:,}", "cyan"),
        ("کل سفارش‌ها", await db.orders.count_documents({}), "blue"),
        ("نیازمند تحویل دستی", manual_pending, "orange"),
        ("در انتظار پرداخت", status_counts[S.PENDING_PAYMENT], "orange"),
        ("ناموفق", status_counts[S.FAILED], "red"),
        ("موجودی Stars ربات", sup.get("bot_balance") if sup.get("bot_balance") is not None else "—", "cyan"),
        ("کاربران", await db.users.count_documents({}), "green"),
        ("موجودی کیف پول‌ها", f"{wallet_total:,}", "blue"),
    ]
    recent = [p async for p in db.payments.find({}).sort("created_at", -1).limit(8)]
    sup_status = [("اتصال به تلگرام", "موفق" if sup.get("ok") else f"خطا: {sup.get('error') or 'هنوز همگام نشده'}"),
                  ("آخرین همگام‌سازی", str(sup.get("checked_at") or "—")[:16])]
    return render(request, admin, "overview.html", stats=stats, daily=daily, status_counts=status_counts,
                  recent=recent, sup_status=sup_status, STATUS_FA=STATUS_FA)


# ---------------- products / categories ----------------
KIND_TITLES = {"stars": "Telegram Stars", "premium": "Telegram Premium", "gift": "Telegram Gifts", "other": "سایر"}
CURRENCIES = [("IRT", "تومان"), ("IRR", "ریال"), ("USD", "دلار (USD)"), ("XTR", "Stars (قیمت خرید هر Star از تنظیمات)")]


def _int_str(value) -> str:
    """'200000.00' -> '200,000' (display only)."""
    try:
        return f"{int(Decimal(str(value)).to_integral_value()):,}"
    except Exception:
        return "—"


def _product_row(p: dict, q: dict | None) -> list:
    sup = p.get("supply") or {}
    kind = p.get("kind")
    if kind == "stars":
        amount = f"{p.get('amount') or '—'} Stars"
    elif kind == "premium":
        amount = f"{p.get('amount') or '—'} ماه"
    elif kind == "gift":
        amount = f"{sup['stars_cost']} Stars" if sup.get("stars_cost") else "—"
    else:
        amount = "—"
    stock = "نامحدود" if p.get("stock") is None else str(p["stock"])
    if sup.get("remaining") is not None:
        stock += f" (تلگرام: {sup['remaining']})"
    status = "فعال" if p.get("enabled") else "غیرفعال"
    err = (q or {}).get("error") or sup.get("error")
    if err:
        status += f" ⚠ {err}"
    pid = p["public_id"]
    buttons = [
        {"link": {"href": f"/products/{pid}", "label": "ویرایش"}},
        {"form": {"action": f"/products/{pid}/toggle", "label": "غیرفعال کن" if p.get("enabled") else "فعال کن",
                  "confirm": ""}},
    ]
    return [p["name"], amount, DELIVERY_FA.get(p.get("delivery") or "manual", "دستی"),
            _int_str(q["cost_toman"]) if q and q.get("cost_toman") else "—",
            _int_str(q["final_toman"]) if q and q.get("final_toman") else "—",
            (q or {}).get("stars_amount") or "—", stock, status,
            str((q or {}).get("updated_at") or "")[:16] or "—", buttons]


def _product_form(action: str, title: str, cats, v: dict | None = None) -> dict:
    v = v or {}
    pricing = v.get("pricing") or {}
    sup = v.get("supply") or {}
    return {"action": action, "title": title, "submit": "ذخیره", "fields": [
        form_field("name", "نام محصول", v.get("name", ""), required=True),
        form_field("description", "توضیحات", v.get("description", "")),
        form_field("category_id", "دسته‌بندی", v.get("category_id") or "", "select", [("", "—")] + cats),
        form_field("kind", "نوع", v.get("kind", "stars"), "select",
                   [("stars", "Stars"), ("premium", "Premium"), ("gift", "Gift"), ("other", "سایر")]),
        form_field("amount", "مقدار (Stars: تعداد Star · Premium: ۳ یا ۶ یا ۱۲ ماه · Gift: خالی)",
                   "" if v.get("amount") is None else str(v["amount"])),
        form_field("gift_id", "شناسه گیفت (فقط Gift؛ بهتر است از دکمه «دریافت گیفت‌ها» استفاده کنید)", v.get("gift_id") or ""),
        form_field("delivery_mode", "روش تحویل (Stars همیشه دستی است)",
                   "manual" if v and (v.get("delivery") or "manual") == "manual" else "auto", "select",
                   [("auto", "خودکار توسط ربات (Premium / Gift)"), ("manual", "دستی (شما تحویل می‌دهید)")]),
        form_field("price_source", "منبع قیمت خرید", pricing.get("source") or "manual", "select",
                   [("telegram", "خودکار از تلگرام (Premium / Gift)"), ("manual", "دستی")]),
        form_field("manual_amount", "قیمت خرید دستی (خالی در Stars = تعداد Star × قیمت خرید هر Star)",
                   str(pricing.get("manual_amount") or "")),
        form_field("manual_currency", "ارز قیمت خرید دستی", pricing.get("manual_currency") or "IRT", "select", CURRENCIES),
        form_field("profit_toman", "سود اختصاصی (۰ تا ۷۰۰۰ تومان؛ خالی = سود پیش‌فرض)", str(v.get("profit_toman") or "")),
        form_field("stock", "موجودی (خالی = نامحدود)", "" if v.get("stock") is None else str(v["stock"])),
        form_field("enabled", "وضعیت", "1" if v.get("enabled", False) else "0", "select",
                   [("1", "فعال"), ("0", "غیرفعال")]),
    ]}


def _parse_product(f: dict) -> dict:
    """Form values -> product document. Raises ValueError with a Persian message for the admin."""
    kind = f["kind"]
    price_source = f["price_source"]
    if price_source == "manual" and not f["manual_amount"].strip() and kind in ("premium", "gift"):
        price_source = "telegram"      # no manual price typed: Premium/Gift use Telegram's own price
    if kind not in KIND_TITLES:
        raise ValueError("نوع محصول نامعتبر است")
    amount = None
    if f["amount"].strip():
        if not f["amount"].strip().isdigit() or int(f["amount"]) < 1:
            raise ValueError("مقدار باید یک عدد صحیح مثبت باشد")
        amount = int(f["amount"])
    gift_id = f["gift_id"].strip() or None
    auto = f["delivery_mode"] == "auto"
    pricing: dict
    manual_amount = f["manual_amount"].strip()
    if kind == "stars":
        if not amount:
            raise ValueError("برای Stars تعداد Star را وارد کنید")
        delivery, gift_id = "stars_auto", None      # automatic only if a real sender exists (none today) -> manual
        if price_source == "telegram":
            raise ValueError("قیمت Stars از تلگرام خوانده نمی‌شود؛ منبع قیمت را «دستی» بگذارید")
    elif kind == "premium":
        if amount not in PREMIUM_STARS:
            raise ValueError("برای Premium مقدار باید ۳، ۶ یا ۱۲ (ماه) باشد")
        delivery, gift_id = ("auto_premium" if auto else "manual"), None
    elif kind == "gift":
        if not gift_id:
            raise ValueError("شناسه گیفت لازم است")
        delivery = "auto_gift" if auto else "manual"
    else:
        delivery, gift_id = "manual", None
        if price_source == "telegram":
            raise ValueError("برای این نوع فقط قیمت دستی ممکن است")
    if price_source == "telegram":
        pricing = {"source": "telegram"}
    else:
        if not manual_amount:
            if kind != "stars":
                raise ValueError("قیمت خرید دستی را وارد کنید")
            pricing = {"source": "manual", "manual_amount": str(amount), "manual_currency": "XTR"}
        else:
            if to_decimal(manual_amount) <= 0:
                raise ValueError("قیمت خرید باید مثبت باشد")
            if f["manual_currency"] not in dict(CURRENCIES):
                raise ValueError("ارز نامعتبر است")
            pricing = {"source": "manual", "manual_amount": str(to_decimal(manual_amount)),
                       "manual_currency": f["manual_currency"]}
    profit = f["profit_toman"].strip()
    if profit:
        profit = str(validate_profit(profit))
    stock = f["stock"].strip()
    if stock:
        if not stock.isdigit():
            raise ValueError("موجودی باید عدد صحیح (۰ یا بیشتر) یا خالی باشد")
        stock_val = int(stock)
    else:
        stock_val = None
    return {"name": f["name"].strip(), "description": f["description"].strip(), "category_id": f["category_id"] or None,
            "kind": kind, "amount": amount, "gift_id": gift_id, "delivery": delivery, "pricing": pricing,
            "supplier_key": "telegram" if delivery in AUTO_DELIVERIES else ("stars" if delivery == "stars_auto" else "manual"), "profit_toman": profit or None,
            "stock": stock_val, "enabled": f["enabled"] == "1", "digital_in_telegram": True}


PRODUCT_FIELDS = ("name", "description", "category_id", "kind", "amount", "gift_id", "delivery_mode", "price_source",
                  "manual_amount", "manual_currency", "profit_toman", "stock", "enabled")


async def _read_product_form(request: Request) -> dict:
    form = await request.form()
    return {k: str(form.get(k) or "") for k in PRODUCT_FIELDS}


@router.get("/products", response_class=HTMLResponse)
async def products_page(request: Request, admin=Depends(auth.current_admin)):
    need_any(admin, "products")
    db = get_db()
    cats = [(c["public_id"], c["name"]) async for c in db.categories.find({})]
    groups: dict[str, list] = {k: [] for k in KIND_TITLES}
    async for p in db.products.find({"hidden": {"$ne": True}}).sort("created_at", -1):
        q = await db.price_quotes.find_one({"product_id": p["public_id"]})
        groups.setdefault(p.get("kind") if p.get("kind") in KIND_TITLES else "other", []).append(_product_row(p, q))
    cols = ["نام", "مقدار", "تحویل", "قیمت خرید (تومان)", "قیمت فروش (تومان)", "Stars مشتری", "موجودی", "وضعیت",
            "آخرین به‌روزرسانی قیمت", "عملیات"]
    tables = [{"title": f"{KIND_TITLES[k]} ({len(rows)})", "columns": cols, "rows": rows} for k, rows in groups.items()
              if rows or k != "other"]
    actions = [{"action": "/products/import-gifts", "label": "دریافت گیفت‌ها از تلگرام"},
               {"action": "/products/create-premium", "label": "ساخت پکیج‌های Premium (۳/۶/۱۲ ماه)"}]
    return render(request, admin, "page.html", title="محصولات", actions=actions,
                  forms=[_product_form("/products/new", "افزودن محصول", cats)], tables=tables)


@router.get("/products/{pid}", response_class=HTMLResponse)
async def product_edit_page(pid: str, request: Request, admin=Depends(auth.current_admin)):
    need_any(admin, "products")
    db = get_db()
    p = await db.products.find_one({"public_id": pid})
    if not p:
        return back(request, "/products", "محصول پیدا نشد.")
    cats = [(c["public_id"], c["name"]) async for c in db.categories.find({})]
    q = await db.price_quotes.find_one({"product_id": pid}) or {}
    sup = p.get("supply") or {}
    info = [["قیمت خرید (تومان)", _int_str(q["cost_toman"]) if q.get("cost_toman") else "—"],
            ["قیمت فروش (تومان)", _int_str(q["final_toman"]) if q.get("final_toman") else "—"],
            ["سود (تومان)", _int_str(q["effective_profit_toman"]) if q.get("effective_profit_toman") else "—"],
            ["Stars برای مشتری", q.get("stars_amount") or "—"],
            ["آخرین به‌روزرسانی قیمت", str(q.get("updated_at") or "—")[:19]],
            ["خطای قیمت", q.get("error") or "—"],
            ["قیمت از تلگرام (Stars)", sup.get("stars_cost") or "—"],
            ["آخرین همگام‌سازی با تلگرام", str(sup.get("checked_at") or "—")[:19]],
            ["خطای تلگرام", sup.get("error") or "—"]]
    return render(request, admin, "page.html", title=f"ویرایش: {p['name']}",
                  forms=[_product_form(f"/products/{pid}/save", "ویرایش محصول", cats, p)],
                  tables=[{"title": "وضعیت قیمت و تأمین", "columns": ["مورد", "مقدار"], "rows": info}])


async def _save_product_from_request(request: Request, admin: dict, csrf: str, pid: str | None):
    auth.check_csrf(request, csrf)
    need_any(admin, "products")
    try:
        data = _parse_product(await _read_product_form(request))
    except (ValueError, MoneyError) as exc:
        return back(request, f"/products/{pid}" if pid else "/products", f"خطا: {exc}")
    pid = await catalog.save_product(data, pid)
    await audit(admin["username"], "product_save", pid, {"name": data["name"], "enabled": data["enabled"]})
    await hooks.recompute_all()
    return back(request, "/products", "محصول ذخیره شد.")


@router.post("/products/new")
async def product_create(request: Request, admin=Depends(auth.current_admin), csrf: str = Form("")):
    return await _save_product_from_request(request, admin, csrf, None)


@router.post("/products/import-gifts")
async def products_import_gifts(request: Request, admin=Depends(auth.current_admin), csrf: str = Form("")):
    auth.check_csrf(request, csrf)
    need_any(admin, "products")
    bot = get_bot()
    if bot is None:
        return back(request, "/products", "ربات فعال نیست (BOT_TOKEN).")
    try:
        n = await telegram_supply.import_gifts(bot)
    except Exception as exc:
        return back(request, "/products", f"خطا در دریافت گیفت‌ها: {redact(f'{type(exc).__name__}: {exc}')}")
    await audit(admin["username"], "import_gifts", "", {"created": n})
    return back(request, "/products", f"{n} گیفت جدید اضافه شد (غیرفعال است؛ هر کدام را که می‌خواهید بفروشید فعال کنید).")


@router.post("/products/create-premium")
async def products_create_premium(request: Request, admin=Depends(auth.current_admin), csrf: str = Form("")):
    auth.check_csrf(request, csrf)
    need_any(admin, "products")
    n = await telegram_supply.create_premium_products()
    if get_bot() is not None:
        await telegram_supply.sync_supply(get_bot())
    await hooks.recompute_all()
    await audit(admin["username"], "create_premium", "", {"created": n})
    return back(request, "/products", f"{n} پکیج Premium اضافه شد (غیرفعال است؛ فعالش کنید).")


@router.post("/products/{pid}/save")
async def product_update(pid: str, request: Request, admin=Depends(auth.current_admin), csrf: str = Form("")):
    return await _save_product_from_request(request, admin, csrf, pid)


@router.post("/products/{pid}/toggle")
async def product_toggle(pid: str, request: Request, admin=Depends(auth.current_admin), csrf: str = Form("")):
    auth.check_csrf(request, csrf)
    need_any(admin, "products")
    p = await get_db().products.find_one({"public_id": pid})
    if p:
        await get_db().products.update_one({"public_id": pid}, {"$set": {"enabled": not p.get("enabled")}})
        await audit(admin["username"], "product_toggle", pid, {"enabled": not p.get("enabled")})
    return back(request, "/products", "وضعیت محصول تغییر کرد.")


@router.get("/categories", response_class=HTMLResponse)
async def categories_page(request: Request, admin=Depends(auth.current_admin)):
    need_any(admin, "products")
    rows = [[c["name"], "فعال" if c.get("enabled") else "غیرفعال", c["public_id"][:8]] async for c in get_db().categories.find({})]
    form = {"action": "/categories/save", "title": "افزودن دسته‌بندی", "submit": "ذخیره",
            "fields": [form_field("name", "نام", required=True)]}
    return render(request, admin, "page.html", title="دسته‌بندی‌ها", forms=[form],
                  tables=[{"title": "دسته‌بندی‌ها", "columns": ["نام", "وضعیت", "شناسه"], "rows": rows}])


@router.post("/categories/save")
async def categories_save(request: Request, admin=Depends(auth.current_admin), csrf: str = Form(""), name: str = Form(...)):
    auth.check_csrf(request, csrf)
    need_any(admin, "products")
    await catalog.save_category(name.strip())
    await audit(admin["username"], "category_save", name)
    return back(request, "/categories", "ذخیره شد.")


# ---------------- pricing ----------------
def _age(ts) -> str:
    ts = as_aware(ts)
    if ts is None:
        return "—"
    mins = int((utcnow() - ts).total_seconds() // 60)
    return f"{mins} دقیقه پیش" if mins < 120 else f"{mins // 60} ساعت پیش"


@router.get("/pricing", response_class=HTMLResponse)
async def pricing_page(request: Request, admin=Depends(auth.current_admin)):
    need_any(admin, "pricing", "pricing_view")
    db = get_db()
    s = await st.all_settings()
    rows = []
    async for q in db.price_quotes.find({}):
        p = await db.products.find_one({"public_id": q["product_id"]})
        rows.append([(p or {}).get("name", q["product_id"][:8]), q.get("base_amount") or "—", q.get("base_currency") or "",
                     q.get("rate_toman_per_unit") or "—", _int_str(q["cost_toman"]) if q.get("cost_toman") else "—",
                     q.get("profit_toman") or "—", _int_str(q["final_toman"]) if q.get("final_toman") else "—",
                     q.get("stars_amount") or "—", q.get("stars_profit_toman") or "—",
                     str(q.get("updated_at") or "")[:16], q.get("error") or "OK"])
    rates = [[r["_id"], r.get("toman_per_unit") or "—", r.get("source") or "—", _age(r.get("fetched_at")),
              r.get("last_error") or "OK"] async for r in db.exchange_rates.find({})]
    sup = await telegram_supply.get_supply_status()
    status_rows = [
        ["به‌روزرسانی خودکار", "روشن" if s["auto_refresh_enabled"] == "true" else "خاموش"],
        ["تحویل خودکار Premium/Gift", "روشن" if s["auto_delivery_enabled"] == "true" else "خاموش (سفارش‌ها دستی می‌مانند)"],
        ["همگام‌سازی با تلگرام", f"{_age(sup.get('checked_at'))} — " + ("موفق" if sup.get("ok") else f"خطا: {sup.get('error') or 'هنوز انجام نشده'}")],
        ["موجودی Stars ربات", sup.get("bot_balance") if sup.get("bot_balance") is not None else (sup.get("balance_error") or "—")],
        ["منبع خودکار نرخ دلار", "تنظیم شده" if get_config().rate_api_url else "تنظیم نشده (نرخ را دستی وارد کنید)"],
    ]
    editable = can(admin, "pricing")
    fields = [form_field("profit_toman", "سود پیش‌فرض (۰ تا ۷٬۰۰۰ تومان)", s["profit_toman"]),
              form_field("rounding_step", "گام گرد کردن (تومان)", s["rounding_step"]),
              form_field("rounding_mode", "نوع گرد کردن", s["rounding_mode"], "select", [("up", "به بالا"), ("down", "به پایین"), ("nearest", "نزدیک‌ترین")]),
              form_field("stars_usd_value", "ارزش هر Star برای مشتری (دلار، مثلاً 0.02)", s["stars_usd_value"] or ""),
              form_field("stars_cost_usd", "قیمت خرید هر Star برای شما (دلار)", s["stars_cost_usd"] or ""),
              form_field("stars_payout_usd_value", "دریافتی شما از هر Star (دلار، اختیاری)", s["stars_payout_usd_value"] or ""),
              form_field("auto_refresh_enabled", "به‌روزرسانی خودکار قیمت/موجودی", s["auto_refresh_enabled"], "select", [("true", "روشن"), ("false", "خاموش")]),
              form_field("refresh_interval_minutes", "بازه به‌روزرسانی (دقیقه، حداقل ۵)", s["refresh_interval_minutes"]),
              form_field("max_age_minutes", "حداکثر عمر نرخ/داده خودکار (دقیقه) — بعدش فروش متوقف می‌شود", s["max_age_minutes"]),
              form_field("manual_rate_max_age_minutes", "حداکثر عمر نرخ دستی (دقیقه) — بعدش فروش متوقف می‌شود", s["manual_rate_max_age_minutes"]),
              form_field("stars_auto_min", "حداقل Stars برای تحویل خودکار Stars (فقط وقتی روش رسمی ارسال وجود داشته باشد)", s["stars_auto_min"]),
              form_field("auto_delivery_enabled", "تحویل خودکار Premium/Gift", s["auto_delivery_enabled"], "select", [("true", "روشن"), ("false", "خاموش")]),
              form_field("order_ttl_minutes", "مهلت پرداخت سفارش (دقیقه)", s["order_ttl_minutes"])]
    forms = []
    if editable:
        forms = [{"action": "/pricing/manual-rate", "title": "ثبت نرخ دلار به‌صورت دستی (تومان برای هر ۱ دلار)", "submit": "ثبت نرخ",
                  "fields": [form_field("rate", "نرخ دلار (تومان)", required=True)]},
                 {"action": "/pricing/settings", "title": "تنظیمات قیمت‌گذاری", "submit": "ذخیره", "fields": fields}]
    actions = [{"action": "/pricing/refresh", "label": "به‌روزرسانی همین حالا"}] if editable else []
    return render(request, admin, "page.html", title="قیمت‌گذاری", forms=forms, actions=actions, tables=[
        {"title": "وضعیت منابع", "columns": ["مورد", "وضعیت"], "rows": status_rows},
        {"title": "نرخ‌های ارز (تومان برای هر ۱ واحد)", "columns": ["ارز", "نرخ (تومان)", "منبع", "زمان دریافت", "وضعیت"], "rows": rates},
        {"title": "قیمت‌های محاسبه‌شده", "columns": ["محصول", "قیمت پایه", "ارز", "نرخ", "قیمت خرید (تومان)", "سود تنظیمی", "قیمت فروش (تومان)", "Stars", "سود واقعی با Stars (تومان)", "آخرین به‌روزرسانی", "وضعیت"], "rows": rows}])


@router.post("/pricing/manual-rate")
async def pricing_manual_rate(request: Request, admin=Depends(auth.current_admin), csrf: str = Form(""), rate: str = Form("")):
    auth.check_csrf(request, csrf)
    need_any(admin, "pricing")
    try:
        await engine.set_manual_rate("USD", rate.strip().replace(",", "").replace("٬", ""))
    except MoneyError as exc:
        return back(request, "/pricing", f"خطا: {exc}")
    await audit(admin["username"], "manual_usd_rate", "USD", {"rate": rate.strip()})
    await hooks.recompute_all()
    return back(request, "/pricing", "نرخ دلار ثبت شد و قیمت‌ها دوباره محاسبه شدند.")


@router.post("/pricing/settings")
async def pricing_settings(request: Request, admin=Depends(auth.current_admin), csrf: str = Form("")):
    auth.check_csrf(request, csrf)
    need_any(admin, "pricing")
    form = await request.form()
    changed = []
    try:
        for key in ("profit_toman", "rounding_step", "rounding_mode", "refresh_interval_minutes", "max_age_minutes",
                    "manual_rate_max_age_minutes", "order_ttl_minutes", "stars_auto_min", "stars_usd_value", "stars_cost_usd",
                    "stars_payout_usd_value", "auto_refresh_enabled", "auto_delivery_enabled"):
            val = (form.get(key) or "").strip()
            if val == "" and key in ("stars_usd_value", "stars_cost_usd", "stars_payout_usd_value"):
                continue
            if val and val != (await st.get_setting(key) or ""):
                await st.set_setting(key, val)
                changed.append(key)
    except MoneyError as exc:
        return back(request, "/pricing", f"خطا: {exc}")
    await audit(admin["username"], "pricing_settings", ",".join(changed))
    await hooks.recompute_all()
    return back(request, "/pricing", "تنظیمات ذخیره شد و قیمت‌ها از داده‌های موجود دوباره محاسبه شدند.")


@router.post("/pricing/refresh")
async def pricing_refresh(request: Request, admin=Depends(auth.current_admin), csrf: str = Form("")):
    auth.check_csrf(request, csrf)
    need_any(admin, "pricing")
    report = await hooks.refresh_now()
    await audit(admin["username"], "pricing_refresh")
    return back(request, "/pricing", "به‌روزرسانی اجرا شد (نتیجه را در جدول‌ها ببینید)." if report else "به‌روزرسانی انجام نشد (قفل فعال است).")


# ---------------- orders ----------------
MANUAL_FILTER = "MANUAL"
PAY_STATUS_FA = {"UNPAID": "پرداخت‌نشده", "PAID": "پرداخت‌شده", "REFUNDED": "بازپرداخت‌شده"}
DELIVERY_STATUS_FA = {"NONE": "شروع نشده", "PROCESSING": "در حال تحویل", "MANUAL_PENDING": "نیازمند تحویل دستی",
                      "DONE": "تحویل‌شده", "FAILED": "تحویل ناموفق"}
METHOD_FA = {"stars": "Stars", "wallet": "کیف پول", "mock": "آزمایشی"}


@router.get("/orders", response_class=HTMLResponse)
async def orders_page(request: Request, status: str = "", q: str = "", pay: str = "", deliv: str = "", page: int = 1,
                      admin=Depends(auth.current_admin)):
    need_any(admin, "orders_view", "orders")
    flt: dict = {}
    if status == MANUAL_FILTER:
        flt = {"status": S.PROCESSING, "fulfillment_status": "MANUAL_PENDING"}
    elif status in STATUS_FA:
        flt["status"] = status
    if pay in PAY_STATUS_FA:
        flt["payment_status"] = pay
    if deliv in DELIVERY_STATUS_FA:
        flt["fulfillment_status"] = deliv
    if q:
        flt["$or"] = [{"public_id": {"$regex": "^" + re.escape(q[:32])}}, {"user_id": int(q)} if q.isdigit() else {"public_id": "x"}]
    per = 25
    db = get_db()
    total = await db.orders.count_documents(flt)
    items = [o async for o in db.orders.find(flt).sort("created_at", -1).skip((max(page, 1) - 1) * per).limit(per)]
    names = {u["telegram_id"]: u.get("username") async for u in db.users.find({"telegram_id": {"$in": [o["user_id"] for o in items]}})}
    rows = []
    for o in items:
        btns = [{"link": {"href": f"/orders/{o['public_id']}", "label": "جزئیات"}}]
        if can(admin, "orders"):
            if o["status"] == S.FAILED:
                btns.append({"form": {"action": f"/orders/{o['public_id']}/retry", "label": "تلاش مجدد تحویل", "confirm": "تحویل دوباره انجام شود؟"}})
            if o["status"] == S.PROCESSING:
                btns.append({"form": {"action": f"/orders/{o['public_id']}/deliver", "label": "تحویل شد", "confirm": "تحویل ثبت شود؟ (مطمئن شوید محصول را تحویل داده‌اید)", "text_field": "delivery_text"}})
        if can(admin, "refunds") and o["status"] in (S.PAID, S.PROCESSING, S.FAILED, S.FULFILLED):
            btns.append({"form": {"action": f"/orders/{o['public_id']}/refund", "text_field": "reason",
                                  "label": "بازپرداخت به کیف پول (دلیل را بنویسید)" if o.get("payment_method") == "wallet" else "لغو و بازپرداخت Stars (دلیل را بنویسید)",
                                  "confirm": "بازپرداخت انجام و غیرقابل بازگشت است. ادامه؟", "danger": True}})
        spec = o.get("delivery_spec") or {}
        uname = names.get(o["user_id"])
        rows.append([o["public_id"][:8], (f"@{uname} " if uname else "") + str(o["user_id"]) + (f" ← @{o['target_username']}" if o.get("target_username") else ""),
                     f"{o['product_name']} × {o['quantity']}", DELIVERY_FA.get(spec.get("delivery") or "manual", "دستی"),
                     f"{o.get('amount', o.get('stars_amount')) or '—'} {o.get('currency', 'XTR')}",
                     METHOD_FA.get(o.get("payment_method"), o.get("payment_method")),
                     PAY_STATUS_FA.get(o.get("payment_status"), o.get("payment_status")),
                     DELIVERY_STATUS_FA.get(o.get("fulfillment_status"), o.get("fulfillment_status")),
                     STATUS_FA.get(o["status"], o["status"]),
                     o.get("fulfillment_error") or o.get("manual_reason") or "", str(o["created_at"])[:16], btns])
    statuses = {**STATUS_FA, MANUAL_FILTER: "⚠ نیازمند تحویل دستی"}
    base = f"/orders?status={status}&q={q}&pay={pay}&deliv={deliv}"
    return render(request, admin, "page.html", title="سفارش‌ها", forms=[],
                  filters={"status": status, "q": q, "statuses": statuses, "pay": pay, "deliv": deliv,
                           "pay_statuses": PAY_STATUS_FA, "deliv_statuses": DELIVERY_STATUS_FA},
                  pager={"page": page, "pages": (total + per - 1) // per, "base": base},
                  tables=[{"title": f"{total} سفارش", "columns": ["شناسه", "گیرنده", "محصول", "تحویل", "مبلغ پرداخت", "روش", "وضعیت پرداخت", "وضعیت تحویل", "وضعیت سفارش", "خطا / علت", "زمان", "عملیات"], "rows": rows}])


@router.get("/orders/{oid}", response_class=HTMLResponse)
async def order_detail_page(oid: str, request: Request, admin=Depends(auth.current_admin)):
    need_any(admin, "orders_view", "orders")
    db = get_db()
    o = await db.orders.find_one({"public_id": oid})
    if not o:
        return back(request, "/orders", "سفارش پیدا نشد.")
    user = await db.users.find_one({"telegram_id": o["user_id"]}) or {}
    pay = [p async for p in db.payments.find({"order_id": oid})]
    sup = await db.supplier_orders.find_one({"order_id": oid}) or {}
    snap = o.get("price_snapshot") or {}
    ts = lambda v: str(v)[:19] if v else "—"
    info = [["شناسه سفارش", oid], ["مشتری", f"{('@' + user['username'] + ' ') if user.get('username') else ''}{o['user_id']}"],
            ["محصول × تعداد", f"{o['product_name']} × {o['quantity']}"],
            ["مبلغ پرداخت", f"{o.get('amount', o.get('stars_amount')) or '—'} {o.get('currency', 'XTR')}"],
            ["قیمت تومانی ثابت‌شده (snapshot)", f"{o['total_toman']:,} تومان"],
            ["قیمت خرید در لحظه سفارش (تومان)", _int_str(snap["cost_toman"]) if snap.get("cost_toman") else "—"],
            ["روش پرداخت", METHOD_FA.get(o.get("payment_method"), o.get("payment_method"))],
            ["وضعیت پرداخت", PAY_STATUS_FA.get(o.get("payment_status"), o.get("payment_status"))],
            ["وضعیت تحویل", DELIVERY_STATUS_FA.get(o.get("fulfillment_status"), o.get("fulfillment_status"))],
            ["وضعیت سفارش", STATUS_FA.get(o["status"], o["status"])],
            ["نام فنی وضعیت", canonical_status(o)],
            ["حساب مقصد (Stars)", f"@{o['target_username']}" if o.get("target_username") else "—"],
            ["شناسه پیگیری تحویل", o.get("delivery_ref") or "—"],
            ["دلیل بازپرداخت", o.get("refund_reason") or "—"],
            ["زمان ایجاد", ts(o.get("created_at"))], ["زمان پرداخت", ts(o.get("paid_at"))],
            ["زمان تحویل", ts(o.get("completed_at"))],
            ["شناسه پیگیری پرداخت", o.get("paid_charge_id") or "—"],
            ["علت تحویل دستی", o.get("manual_reason") or "—"], ["خطای تحویل", o.get("fulfillment_error") or "—"],
            ["تلاش‌های تحویل", sup.get("attempts", 0)], ["وضعیت ارسال", sup.get("send_state") or "—"]]
    return render(request, admin, "page.html", title=f"سفارش {oid[:8]}", forms=[], tables=[
        {"title": "اطلاعات سفارش", "columns": ["مورد", "مقدار"], "rows": info},
        {"title": "پرداخت‌ها", "columns": ["شناسه", "روش", "مبلغ", "وضعیت", "زمان"],
         "rows": [[p["_id"][:30], p["provider"], f"{p['amount']} {p['currency']}", p["status"], ts(p.get("created_at"))] for p in pay]},
        {"title": "تاریخچه تغییر وضعیت", "columns": ["زمان", "وضعیت", "یادداشت"],
         "rows": [[ts(h.get("at")), STATUS_FA.get(h.get("to"), h.get("to")), h.get("note", "")] for h in o.get("history", [])]}])


@router.get("/errors", response_class=HTMLResponse)
async def errors_page(request: Request, admin=Depends(auth.current_admin)):
    need_any(admin, "orders_view", "orders", "payments")
    db = get_db()
    ts = lambda v: str(v)[:16] if v else "—"
    pay_err = [[ts(p.get("created_at")), p["_id"][:28], p.get("order_id", "")[:8], p["status"], f"{p['amount']} {p['currency']}"]
               async for p in db.payments.find({"status": {"$in": ["MISMATCH", "NEEDS_REFUND", "ORPHAN"]}}).sort("created_at", -1).limit(50)]
    deliv_err = [[ts(o.get("updated_at")), o["public_id"][:8], o["product_name"],
                  STATUS_FA.get(o["status"], o["status"]), o.get("fulfillment_error") or o.get("manual_reason") or ""]
                 async for o in db.orders.find({"$or": [{"status": S.FAILED}, {"fulfillment_status": "MANUAL_PENDING"}]}).sort("updated_at", -1).limit(50)]
    return render(request, admin, "page.html", title="خطاهای پرداخت و تحویل", forms=[], tables=[
        {"title": "مشکل‌های پرداخت (عدم تطابق، پرداخت بدون سفارش، نیازمند بازپرداخت)", "columns": ["زمان", "شناسه پرداخت", "سفارش", "وضعیت", "مبلغ"], "rows": pay_err},
        {"title": "مشکل‌های تحویل (ناموفق یا نیازمند تحویل دستی)", "columns": ["زمان", "سفارش", "محصول", "وضعیت", "علت / خطا"], "rows": deliv_err}])


@router.post("/orders/{oid}/retry")
async def order_retry(oid: str, request: Request, admin=Depends(auth.current_admin), csrf: str = Form("")):
    auth.check_csrf(request, csrf)
    need_any(admin, "orders")
    r = await fulfillment.fulfill_order(oid)
    await audit(admin["username"], "order_retry", oid, {"result": r})
    return back(request, "/orders", f"نتیجه: {r}")


@router.post("/orders/{oid}/deliver")
async def order_deliver(oid: str, request: Request, admin=Depends(auth.current_admin), csrf: str = Form(""), delivery_text: str = Form("")):
    auth.check_csrf(request, csrf)
    need_any(admin, "orders")
    existing = await orders.get_order(oid)
    if existing and existing.get("target_username") and not delivery_text.strip():
        return back(request, "/orders", "برای سفارش Stars ثبت «شناسه پیگیری تحویل» الزامی است.")
    ok = await fulfillment.complete_manual(oid, admin["username"], delivery_text.strip())
    await audit(admin["username"], "order_deliver", oid, {"ok": ok})
    return back(request, "/orders", "تحویل ثبت شد." if ok else "ثبت تحویل ممکن نبود.")


@router.post("/orders/{oid}/refund")
async def order_refund(oid: str, request: Request, admin=Depends(auth.current_admin), csrf: str = Form(""), reason: str = Form("")):
    auth.check_csrf(request, csrf)
    need_any(admin, "refunds")
    reason = reason.strip()
    if not reason:
        return back(request, "/orders", "دلیل بازپرداخت الزامی است.")
    order = await orders.get_order(oid)
    ok = False
    if order:
        if order.get("payment_method") == "wallet":
            ok = await get_provider("wallet").refund(order, actor=admin["username"], reason=reason)
        else:
            ok = await get_provider("stars").refund(order, bot=get_bot(), actor=admin["username"])
            if ok:
                await get_db().orders.update_one({"public_id": oid}, {"$set": {"refund_reason": reason[:200], "refunded_by": admin["username"]}})
    await audit(admin["username"], "order_refund", oid, {"ok": ok, "reason": reason[:200]})
    if ok:
        where = "به کیف پول شما" if order.get("payment_method") == "wallet" else "به‌صورت Stars به شما"
        await send_user(order["user_id"], f"مبلغ سفارش {oid[:8]} {where} بازگردانده شد.")
    return back(request, "/orders", "بازپرداخت انجام شد." if ok else "بازپرداخت انجام نشد.")


# ---------------- payments / wallets / users ----------------
@router.get("/payments", response_class=HTMLResponse)
async def payments_page(request: Request, admin=Depends(auth.current_admin)):
    need_any(admin, "payments")
    rows = [[p["_id"][:24], p["provider"], p["order_id"][:8], p["user_id"], f"{p['amount']} {p['currency']}", p["status"], str(p["created_at"])[:16]]
            async for p in get_db().payments.find({}).sort("created_at", -1).limit(100)]
    return render(request, admin, "page.html", title="پرداخت‌ها", forms=[], tables=[
        {"title": "آخرین پرداخت‌ها", "columns": ["شناسه", "روش", "سفارش", "کاربر", "مبلغ", "وضعیت", "زمان"], "rows": rows}])


@router.get("/wallets", response_class=HTMLResponse)
async def wallets_page(request: Request, user: str = "", admin=Depends(auth.current_admin)):
    need_any(admin, "wallets_adjust")
    db = get_db()
    flt = {"user_id": int(user)} if user.strip().isdigit() else {}
    rows = [[w["user_id"], f"{w['balance']:,}"] async for w in db.wallets.find(flt).sort("balance", -1).limit(100)]
    ledger = [[str(l["created_at"])[:16], l["user_id"], l["type"], l["amount"], l["balance_after"], l.get("actor", ""), l.get("note", "")]
              async for l in db.wallet_ledger.find(flt).sort("created_at", -1).limit(200 if flt else 50)]
    if flt:
        rec = await wallet.reconcile(flt["user_id"])
        rows.append(["تطبیق با دفتر کل", "سالم ✔" if rec["ok"] else f"ناهمخوان ✖ (دفتر کل {rec['ledger_sum']:,} / موجودی {rec['balance']:,})"])
    lookup = {"action": "/wallets", "method": "get", "title": "جستجوی کاربر (موجودی + تاریخچه شارژ/برداشت)", "submit": "نمایش",
              "fields": [form_field("user", "شناسه عددی تلگرام", user)]}
    form = {"action": "/wallets/adjust", "title": "تنظیم موجودی (ثبت در دفتر کل)", "submit": "اعمال", "fields": [
        form_field("user_id", "شناسه عددی تلگرام", required=True),
        form_field("amount", "مبلغ تومان (مثبت = شارژ، منفی = کسر)", required=True),
        form_field("note", "توضیح", required=True)]}
    return render(request, admin, "page.html", title="کیف پول‌ها", forms=[lookup, form], tables=[
        {"title": "موجودی‌ها", "columns": ["کاربر", "موجودی (تومان)"], "rows": rows},
        {"title": "دفتر کل (آخرین ۵۰)", "columns": ["زمان", "کاربر", "نوع", "مبلغ", "مانده", "توسط", "توضیح"], "rows": ledger}])


@router.post("/wallets/adjust")
async def wallets_adjust(request: Request, admin=Depends(auth.current_admin), csrf: str = Form(""),
                         user_id: int = Form(...), amount: int = Form(...), note: str = Form(...)):
    auth.check_csrf(request, csrf)
    need_any(admin, "wallets_adjust")           # Support / Order Manager cannot change balances
    if amount == 0:
        return back(request, "/wallets", "مبلغ نمی‌تواند صفر باشد.")
    key = f"adjust:{new_id()}"
    try:
        if amount > 0:
            await wallet.credit(user_id, amount, idem_key=key, kind="ADJUSTMENT", actor=admin["username"], note=note)
        else:
            await wallet.debit(user_id, -amount, idem_key=key, kind="ADJUSTMENT", actor=admin["username"], note=note)
    except wallet.InsufficientFunds:
        return back(request, "/wallets", "موجودی کافی نیست.")
    await audit(admin["username"], "wallet_adjust", str(user_id), {"amount": amount, "note": note})
    return back(request, "/wallets", "ثبت شد.")


# ---------------- wallet top-up (Zarinpal) + wallet settings ----------------
TOPUP_SETTING_KEYS = ("zarinpal_topup_enabled", "topup_min_toman", "topup_max_toman", "topup_presets", "wallet_payment_enabled",
                      "wallet_auto_refund_on_fail", "custom_stars_enabled", "custom_stars_min", "custom_stars_max")


@router.get("/topups", response_class=HTMLResponse)
async def topups_page(request: Request, admin=Depends(auth.current_admin)):
    need_any(admin, "payments")
    db = get_db()
    cfg = get_config()
    s = await st.all_settings()
    ts = lambda v: str(v)[:16] if v else "—"
    rows = []
    async for t in db.topups.find({}).sort("created_at", -1).limit(100):
        btns = []
        if t["status"] != topups.PAID and t.get("authority"):
            btns.append({"form": {"action": f"/topups/{t['public_id']}/recheck", "label": "بررسی مجدد با زرین‌پال",
                                  "confirm": "وضعیت این پرداخت از زرین‌پال پرسیده شود؟ (فقط در صورت پرداخت واقعی شارژ می‌شود)"}})
        rows.append([t["public_id"][:8], t["user_id"], f"{t['amount_toman']:,}", topups.STATUS_FA.get(t["status"], t["status"]),
                     t.get("ref_id") or "—", t.get("last_verify_code", "—"), t.get("error") or "", ts(t.get("created_at")), btns])
    on = lambda v: "روشن" if v == "true" else "خاموش"
    status_rows = [
        ["شارژ با زرین‌پال", "فعال" if await topups.is_enabled() else "غیرفعال (کلید، مرچنت‌کد و آدرس عمومی لازم است)"],
        ["مرچنت‌کد در متغیر محیطی", "تنظیم شده" if cfg.zarinpal_merchant_id else "تنظیم نشده"],
        ["آدرس عمومی (PUBLIC_BASE_URL)", "تنظیم شده" if cfg.base_url else "تنظیم نشده"],
        ["حالت آزمایشی (sandbox)", "بله" if cfg.zarinpal_sandbox else "خیر"],
        ["TESTING_MODE (فقط تست‌کننده)", ("روشن — تست‌کننده‌ها: " + (cfg.tester_user_ids or "⚠ خالی: پرداخت برای همه بسته است")) if cfg.testing_mode else "خاموش"],
        ["تحویل Stars", "دستی" + (" (خودکار هم خاموش است)" if not cfg.auto_stars_delivery else " (روش خودکار رسمی وجود ندارد)")],
        ["کلیدهای محیطی فعال‌کننده", ", ".join(k for k, v in (("ZARINPAL_ENABLED", cfg.zarinpal_enabled), ("WALLET_PAYMENTS_ENABLED", cfg.wallet_payments_enabled), ("CUSTOM_STARS_ENABLED", cfg.custom_stars_enabled)) if v is not None) or "هیچ‌کدام (تنظیمات پنل)"],
        ["پرداخت سفارش از کیف پول", on(s["wallet_payment_enabled"])],
        ["مجموع موجودی کیف پول‌ها", f"{sum([w['balance'] async for w in db.wallets.find({})]):,}"],
    ]
    sel = [("true", "روشن"), ("false", "خاموش")]
    fields = [form_field("zarinpal_topup_enabled", "شارژ کیف پول با زرین‌پال", s["zarinpal_topup_enabled"], "select", sel),
              form_field("topup_min_toman", "حداقل مبلغ شارژ (تومان)", s["topup_min_toman"]),
              form_field("topup_max_toman", "حداکثر مبلغ شارژ (تومان)", s["topup_max_toman"]),
              form_field("topup_presets", "مبلغ‌های پیشنهادی (تومان، با کاما)", s["topup_presets"]),
              form_field("wallet_payment_enabled", "پرداخت سفارش از کیف پول", s["wallet_payment_enabled"], "select", sel),
              form_field("wallet_auto_refund_on_fail", "بازپرداخت خودکار به کیف پول وقتی تحویل قطعاً ناموفق شد (فقط سفارش‌های کیف پول)", s["wallet_auto_refund_on_fail"], "select", sel),
              form_field("custom_stars_enabled", "خرید Stars با تعداد دلخواه", s["custom_stars_enabled"], "select", sel),
              form_field("custom_stars_min", "حداقل تعداد Stars دلخواه", s["custom_stars_min"]),
              form_field("custom_stars_max", "حداکثر تعداد Stars دلخواه", s["custom_stars_max"])]
    form = {"action": "/topups/settings", "title": "تنظیمات کیف پول، شارژ و تعداد دلخواه", "submit": "ذخیره", "fields": fields}
    return render(request, admin, "page.html", title="شارژ و تنظیمات کیف پول", forms=[form], tables=[
        {"title": "وضعیت", "columns": ["مورد", "وضعیت"], "rows": status_rows},
        {"title": "تاریخچه شارژها (آخرین ۱۰۰)", "columns": ["شناسه", "کاربر", "مبلغ (تومان)", "وضعیت", "شماره پیگیری زرین‌پال", "آخرین کد بررسی", "خطا", "زمان", "عملیات"], "rows": rows}])


@router.post("/topups/settings")
async def topups_settings(request: Request, admin=Depends(auth.current_admin), csrf: str = Form("")):
    auth.check_csrf(request, csrf)
    need_any(admin, "payments")
    form = await request.form()
    new = {k: (form.get(k) or "").strip() for k in TOPUP_SETTING_KEYS}
    try:
        lo = int(new["topup_min_toman"] or await st.get_setting("topup_min_toman"))
        hi = int(new["topup_max_toman"] or await st.get_setting("topup_max_toman"))
        clo = int(new["custom_stars_min"] or await st.get_setting("custom_stars_min"))
        chi = int(new["custom_stars_max"] or await st.get_setting("custom_stars_max"))
        if lo > hi or clo > chi:
            return back(request, "/topups", "خطا: حداقل نباید از حداکثر بیشتر باشد.")
        changed = []
        for key, val in new.items():
            if val and val != (await st.get_setting(key) or ""):
                await st.set_setting(key, val)
                changed.append(key)
    except (MoneyError, ValueError) as exc:
        return back(request, "/topups", f"خطا: {exc}")
    await audit(admin["username"], "wallet_settings", ",".join(changed))
    return back(request, "/topups", "تنظیمات ذخیره شد.")


@router.post("/topups/{tid}/recheck")
async def topups_recheck(tid: str, request: Request, admin=Depends(auth.current_admin), csrf: str = Form("")):
    auth.check_csrf(request, csrf)
    need_any(admin, "payments")
    t = await topups.get_topup(tid)
    if not t or not t.get("authority"):
        return back(request, "/topups", "پرداخت پیدا نشد.")
    res = await topups.settle(tid, t["authority"], "OK", from_callback=False)
    await audit(admin["username"], "topup_recheck", tid, {"result": res.code})
    return back(request, "/topups", f"نتیجه: {res.code}")


@router.get("/users", response_class=HTMLResponse)
async def users_page(request: Request, admin=Depends(auth.current_admin)):
    need_any(admin, "users")
    rows = [[u["telegram_id"], u.get("username") or "—", u.get("first_name") or "", "بله" if u.get("referred_by") else "—",
             str(u["created_at"])[:10], "مسدود" if u.get("banned") else "فعال"]
            async for u in get_db().users.find({}).sort("created_at", -1).limit(100)]
    return render(request, admin, "page.html", title="کاربران", forms=[], tables=[
        {"title": "کاربران اخیر", "columns": ["شناسه", "یوزرنیم", "نام", "دعوت‌شده", "عضویت", "وضعیت"], "rows": rows}])


# ---------------- delivery & Telegram status / tickets / audit ----------------
@router.get("/suppliers", response_class=HTMLResponse)
async def suppliers_page(request: Request, admin=Depends(auth.current_admin)):
    need_any(admin, "suppliers")
    db = get_db()
    sup = await telegram_supply.get_supply_status()
    sender = stars_delivery.get_sender()
    sender_line = "غیرفعال: روش رسمی ارسال Stars وجود ندارد؛ همه‌ی سفارش‌های Stars دستی‌اند"
    if sender is not None:
        ok, why = await sender.is_available()
        sender_line = f"فعال ({sender.name})" if ok else f"{sender.name}: {why}"
    status_rows = [
        ["آخرین همگام‌سازی با تلگرام", str(sup.get("checked_at") or "—")[:19]],
        ["وضعیت", "موفق" if sup.get("ok") else f"خطا: {sup.get('error') or 'هنوز انجام نشده'}"],
        ["تعداد گیفت‌های موجود در تلگرام", sup.get("gifts_count") if sup.get("gifts_count") is not None else "—"],
        ["موجودی Stars ربات", sup.get("bot_balance") if sup.get("bot_balance") is not None else (sup.get("balance_error") or "—")],
        ["ارسال خودکار Stars (از ≥ حداقل)", sender_line],
    ]
    errs = [[str(e.get("updated_at"))[:16], e["order_id"][:8], e.get("status", ""), e.get("send_state") or "", e.get("message", "")]
            async for e in db.supplier_orders.find({"$or": [{"status": "FAILED"}, {"send_state": "SENDING"}]}).sort("updated_at", -1).limit(30)]
    return render(request, admin, "page.html", title="تأمین و تحویل", forms=[], tables=[
        {"title": "وضعیت اتصال به تلگرام (تأمین‌کننده شما)", "columns": ["مورد", "مقدار"], "rows": status_rows},
        {"title": "خطاهای تحویل (SENDING = نتیجه ارسال نامشخص؛ دستی بررسی کنید)", "columns": ["زمان", "سفارش", "وضعیت", "ارسال", "پیام"], "rows": errs}])


@router.get("/tickets", response_class=HTMLResponse)
async def tickets_page(request: Request, admin=Depends(auth.current_admin)):
    need_any(admin, "support")
    rows = []
    async for t in get_db().tickets.find({}).sort("created_at", -1).limit(50):
        last = t["messages"][-1]["text"][:80]
        rows.append([t["public_id"][:8], t["user_id"], t["category"], t["status"], last,
                     [{"form": {"action": f"/tickets/{t['public_id']}/reply", "label": "پاسخ", "confirm": "", "text_field": "text"}}]])
    return render(request, admin, "page.html", title="پشتیبانی", forms=[], tables=[
        {"title": "تیکت‌ها", "columns": ["شناسه", "کاربر", "دسته", "وضعیت", "آخرین پیام", "عملیات"], "rows": rows}])


@router.post("/tickets/{tid}/reply")
async def ticket_reply(tid: str, request: Request, admin=Depends(auth.current_admin), csrf: str = Form(""), text: str = Form("")):
    auth.check_csrf(request, csrf)
    need_any(admin, "support")
    t = await get_db().tickets.find_one({"public_id": {"$regex": "^" + re.escape(tid[:32])}})
    if t and text.strip():
        await get_db().tickets.update_one({"public_id": t["public_id"]}, {"$set": {"status": "ANSWERED"},
                                          "$push": {"messages": {"from": "admin", "text": text.strip(), "at": utcnow(), "by": admin["username"]}}})
        await send_user(t["user_id"], f"پاسخ پشتیبانی:\n{text.strip()}")
        await audit(admin["username"], "ticket_reply", tid)
    return back(request, "/tickets", "ارسال شد.")


@router.get("/audit", response_class=HTMLResponse)
async def audit_page(request: Request, admin=Depends(auth.current_admin)):
    need_any(admin, "audit")
    rows = [[str(a["created_at"])[:19], a["actor"], a["action"], a.get("target", ""), str(a.get("details", ""))[:120]]
            async for a in get_db().audit_logs.find({}).sort("created_at", -1).limit(150)]
    return render(request, admin, "page.html", title="لاگ ممیزی", forms=[], tables=[
        {"title": "رویدادها", "columns": ["زمان", "کاربر", "عملیات", "هدف", "جزئیات"], "rows": rows}])
