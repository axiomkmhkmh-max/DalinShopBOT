from __future__ import annotations

import re
from datetime import timedelta
from pathlib import Path

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from constants import STATUS_FA, OrderStatus as S, has_permission
from db import get_db
from money import MoneyError
from payment_manager import get_provider
import background_hooks as hooks
import catalog, fulfillment, orders, wallet
import settings as st
from audit import audit
from notifier import get_bot, send_user
from util import new_id, utcnow
import auth

router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).parent))
templates.env.globals["has_permission"] = has_permission

NAV = [  # (label, path, permission)
    ("نمای کلی", "/", None), ("محصولات", "/products", "products"), ("دسته‌بندی‌ها", "/categories", "products"),
    ("قیمت‌گذاری", "/pricing", "pricing_view"), ("سفارش‌ها", "/orders", "orders_view"),
    ("پرداخت‌ها", "/payments", "payments"), ("کیف پول‌ها", "/wallets", "wallets_adjust"),
    ("کاربران", "/users", "users"), ("تأمین‌کنندگان", "/suppliers", "suppliers"),
    ("پشتیبانی", "/tickets", "support"), ("لاگ ممیزی", "/audit", "audit"),
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
    stats = [
        ("درآمد کل (تومان)", f"{revenue_total:,}", "cyan"),
        ("کل سفارش‌ها", await db.orders.count_documents({}), "blue"),
        ("در انتظار پرداخت", status_counts[S.PENDING_PAYMENT], "orange"),
        ("ناموفق", status_counts[S.FAILED], "red"),
        ("کاربران", await db.users.count_documents({}), "green"),
        ("موجودی کیف پول‌ها", f"{wallet_total:,}", "blue"),
    ]
    recent = [p async for p in db.payments.find({}).sort("created_at", -1).limit(8)]
    sup_status = [(s["name"], "فعال" if s.get("enabled") else "غیرفعال") async for s in db.suppliers.find({})]
    return render(request, admin, "overview.html", stats=stats, daily=daily, status_counts=status_counts,
                  recent=recent, sup_status=sup_status, STATUS_FA=STATUS_FA)


# ---------------- products / categories ----------------
@router.get("/products", response_class=HTMLResponse)
async def products_page(request: Request, admin=Depends(auth.current_admin)):
    need_any(admin, "products")
    db = get_db()
    cats = [(c["public_id"], c["name"]) async for c in db.categories.find({})]
    rows = []
    async for p in db.products.find({}).sort("created_at", -1):
        q = await db.price_quotes.find_one({"product_id": p["public_id"]})
        price = f"{q['final_toman']:,}" if q and q.get("final_toman") else "—"
        rows.append([p["name"], p.get("kind", ""), "فعال" if p.get("enabled") else "غیرفعال", price,
                     p.get("supplier_key") or "manual", p["public_id"][:8]])
    form = {"action": "/products/save", "title": "افزودن محصول", "submit": "ذخیره", "fields": [
        form_field("name", "نام", required=True), form_field("description", "توضیحات"),
        form_field("category_id", "دسته‌بندی", type_="select", options=cats),
        form_field("kind", "نوع", type_="select", options=[("premium", "Premium"), ("stars", "Stars"), ("gift", "Gift"), ("other", "سایر")]),
        form_field("source", "منبع قیمت پایه", type_="select", options=[("manual", "دستی"), ("provider", "Price Provider")]),
        form_field("manual_amount", "قیمت پایه دستی (برای منبع دستی)"),
        form_field("manual_currency", "ارز قیمت پایه", type_="select", options=[("IRT", "تومان"), ("IRR", "ریال"), ("USD", "USD"), ("TON", "TON")]),
        form_field("provider_ref", "شناسه محصول نزد Provider (برای منبع Provider)"),
        form_field("profit_toman", "سود اختصاصی (۰ تا ۷۰۰۰، خالی = پیش‌فرض)"),
        form_field("supplier_key", "کلید تأمین‌کننده", value="manual"),
        form_field("enabled", "فعال", type_="select", options=[("1", "بله"), ("0", "خیر")]),
    ]}
    return render(request, admin, "page.html", title="محصولات", forms=[form] if can(admin, "products") else [],
                  tables=[{"title": "لیست محصولات", "columns": ["نام", "نوع", "وضعیت", "قیمت نهایی", "تأمین‌کننده", "شناسه"], "rows": rows}])


@router.post("/products/save")
async def products_save(request: Request, admin=Depends(auth.current_admin), csrf: str = Form(""),
                        name: str = Form(...), description: str = Form(""), category_id: str = Form(""),
                        kind: str = Form("other"), source: str = Form("manual"), manual_amount: str = Form(""),
                        manual_currency: str = Form("IRT"), provider_ref: str = Form(""),
                        profit_toman: str = Form(""), supplier_key: str = Form("manual"), enabled: str = Form("1")):
    auth.check_csrf(request, csrf)
    need_any(admin, "products")
    try:
        if profit_toman.strip():
            from money import validate_profit
            profit_toman = str(validate_profit(profit_toman))
        pricing = {"source": source, "provider_ref": provider_ref.strip() or None}
        if source == "manual":
            from money import to_decimal
            if to_decimal(manual_amount) <= 0:
                raise MoneyError("قیمت پایه باید مثبت باشد")
            pricing.update({"manual_amount": str(to_decimal(manual_amount)), "manual_currency": manual_currency})
    except MoneyError as exc:
        return back(request, "/products", f"خطا: {exc}")
    pid = await catalog.save_product({
        "name": name.strip(), "description": description.strip(), "category_id": category_id or None, "kind": kind,
        "pricing": pricing, "profit_toman": profit_toman.strip() or None,
        "supplier_key": supplier_key.strip() or "manual", "enabled": enabled == "1", "digital_in_telegram": True})
    await audit(admin["username"], "product_save", pid)
    await hooks.recompute_all()
    return back(request, "/products", "محصول ذخیره شد.")


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
@router.get("/pricing", response_class=HTMLResponse)
async def pricing_page(request: Request, admin=Depends(auth.current_admin)):
    need_any(admin, "pricing", "pricing_view")
    db = get_db()
    s = await st.all_settings()
    rows = []
    async for q in db.price_quotes.find({}):
        p = await db.products.find_one({"public_id": q["product_id"]})
        rows.append([(p or {}).get("name", q["product_id"][:8]), q.get("base_amount") or "—", q.get("base_currency") or "",
                     q.get("rate_toman_per_unit") or "—", q.get("profit_toman") or "—",
                     f"{q['final_toman']:,}" if q.get("final_toman") else "—",
                     q.get("stars_amount") or "—", q.get("seller_receivable_toman") or "—",
                     str(q.get("updated_at") or ""), q.get("error") or "OK"])
    rates = [[r["_id"], r.get("toman_per_unit") or "—", str(r.get("fetched_at") or ""), r.get("last_error") or "OK"]
             async for r in db.exchange_rates.find({})]
    editable = can(admin, "pricing")
    fields = [form_field("profit_toman", "سود (۰ تا ۷٬۰۰۰ تومان)", s["profit_toman"]),
              form_field("rounding_step", "گام گرد کردن (تومان)", s["rounding_step"]),
              form_field("rounding_mode", "نوع گرد کردن", s["rounding_mode"], "select", [("up", "به بالا"), ("down", "به پایین"), ("nearest", "نزدیک‌ترین")]),
              form_field("refresh_interval_minutes", "بازه به‌روزرسانی (دقیقه، حداقل ۵)", s["refresh_interval_minutes"]),
              form_field("max_age_minutes", "حداکثر عمر نرخ/قیمت (دقیقه)", s["max_age_minutes"]),
              form_field("order_ttl_minutes", "مهلت پرداخت سفارش (دقیقه)", s["order_ttl_minutes"]),
              form_field("stars_usd_value", "ارزش هر Star برای مشتری (USD)", s["stars_usd_value"] or ""),
              form_field("stars_payout_usd_value", "دریافتی شما از هر Star (USD، اختیاری)", s["stars_payout_usd_value"] or ""),
              form_field("price_provider", "Price Provider", s["price_provider"], "select", [("http_json", "http_json"), ("mock", "mock (فقط توسعه)")]),
              form_field("rate_provider", "Rate Provider", s["rate_provider"], "select", [("http_json", "http_json"), ("mock", "mock (فقط توسعه)")])]
    forms = [{"action": "/pricing/settings", "title": "تنظیمات قیمت‌گذاری", "submit": "ذخیره", "fields": fields}] if editable else []
    actions = [{"action": "/pricing/refresh", "label": "به‌روزرسانی همین حالا"}] if editable else []
    return render(request, admin, "page.html", title="قیمت‌گذاری خودکار", forms=forms, actions=actions, tables=[
        {"title": "نرخ‌های ارز (Toman per 1 unit)", "columns": ["ارز", "نرخ (تومان)", "زمان دریافت", "وضعیت"], "rows": rates},
        {"title": "قیمت‌های محاسبه‌شده", "columns": ["محصول", "قیمت پایه", "ارز", "نرخ", "سود", "قیمت نهایی", "Stars", "دریافتی شما (تومان)", "آخرین به‌روزرسانی", "وضعیت"], "rows": rows}])


@router.post("/pricing/settings")
async def pricing_settings(request: Request, admin=Depends(auth.current_admin), csrf: str = Form("")):
    auth.check_csrf(request, csrf)
    need_any(admin, "pricing")
    form = await request.form()
    changed = []
    try:
        for key in ("profit_toman", "rounding_step", "rounding_mode", "refresh_interval_minutes", "max_age_minutes",
                    "order_ttl_minutes", "stars_usd_value", "stars_payout_usd_value", "price_provider", "rate_provider"):
            val = (form.get(key) or "").strip()
            if val == "" and key in ("stars_usd_value", "stars_payout_usd_value"):
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
    return back(request, "/pricing", "درخواست به‌روزرسانی اجرا شد." if report else "به‌روزرسانی انجام نشد (قفل یا تنظیمات Provider).")


# ---------------- orders ----------------
@router.get("/orders", response_class=HTMLResponse)
async def orders_page(request: Request, status: str = "", q: str = "", page: int = 1, admin=Depends(auth.current_admin)):
    need_any(admin, "orders_view", "orders")
    flt: dict = {}
    if status in STATUS_FA:
        flt["status"] = status
    if q:
        flt["$or"] = [{"public_id": {"$regex": "^" + re.escape(q[:32])}}, {"user_id": int(q)} if q.isdigit() else {"public_id": "x"}]
    per = 25
    total = await get_db().orders.count_documents(flt)
    rows = []
    async for o in get_db().orders.find(flt).sort("created_at", -1).skip((max(page, 1) - 1) * per).limit(per):
        btns = []
        if can(admin, "orders"):
            if o["status"] == S.FAILED:
                btns.append({"form": {"action": f"/orders/{o['public_id']}/retry", "label": "تلاش مجدد تحویل", "confirm": "تحویل دوباره انجام شود؟"}})
            if o["status"] == S.PROCESSING:
                btns.append({"form": {"action": f"/orders/{o['public_id']}/deliver", "label": "تحویل شد", "confirm": "تحویل دستی ثبت شود؟", "text_field": "delivery_text"}})
        if can(admin, "refunds") and o["status"] in (S.PAID, S.PROCESSING, S.FAILED, S.FULFILLED):
            btns.append({"form": {"action": f"/orders/{o['public_id']}/refund", "label": "بازپرداخت Stars", "confirm": "بازپرداخت انجام و غیرقابل بازگشت است. ادامه؟", "danger": True}})
        rows.append([o["public_id"][:8], o["user_id"], o["product_name"], f"{o['total_toman']:,}", o.get("stars_amount") or "—",
                     STATUS_FA.get(o["status"], o["status"]), o.get("fulfillment_error") or "", str(o["created_at"])[:16], btns])
    return render(request, admin, "page.html", title="سفارش‌ها", forms=[], filters={"status": status, "q": q, "statuses": STATUS_FA},
                  pager={"page": page, "pages": (total + per - 1) // per, "base": f"/orders?status={status}&q={q}"},
                  tables=[{"title": f"{total} سفارش", "columns": ["شناسه", "کاربر", "محصول", "مبلغ (تومان)", "Stars", "وضعیت", "خطا", "زمان", "عملیات"], "rows": rows}])


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
    ok = await fulfillment.complete_manual(oid, admin["username"], delivery_text.strip())
    await audit(admin["username"], "order_deliver", oid, {"ok": ok})
    return back(request, "/orders", "تحویل ثبت شد." if ok else "ثبت تحویل ممکن نبود.")


@router.post("/orders/{oid}/refund")
async def order_refund(oid: str, request: Request, admin=Depends(auth.current_admin), csrf: str = Form("")):
    auth.check_csrf(request, csrf)
    need_any(admin, "refunds")
    order = await orders.get_order(oid)
    ok = False
    if order:
        ok = await get_provider("stars").refund(order, bot=get_bot(), actor=admin["username"])
    await audit(admin["username"], "order_refund", oid, {"ok": ok})
    if ok:
        await send_user(order["user_id"], f"مبلغ سفارش {oid[:8]} به‌صورت Stars به شما بازگردانده شد.")
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
async def wallets_page(request: Request, admin=Depends(auth.current_admin)):
    need_any(admin, "wallets_adjust")
    db = get_db()
    rows = [[w["user_id"], f"{w['balance']:,}"] async for w in db.wallets.find({}).sort("balance", -1).limit(100)]
    ledger = [[str(l["created_at"])[:16], l["user_id"], l["type"], l["amount"], l["balance_after"], l.get("actor", ""), l.get("note", "")]
              async for l in db.wallet_ledger.find({}).sort("created_at", -1).limit(50)]
    form = {"action": "/wallets/adjust", "title": "تنظیم موجودی (ثبت در دفتر کل)", "submit": "اعمال", "fields": [
        form_field("user_id", "شناسه عددی تلگرام", required=True),
        form_field("amount", "مبلغ تومان (مثبت = شارژ، منفی = کسر)", required=True),
        form_field("note", "توضیح", required=True)]}
    return render(request, admin, "page.html", title="کیف پول‌ها", forms=[form], tables=[
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


@router.get("/users", response_class=HTMLResponse)
async def users_page(request: Request, admin=Depends(auth.current_admin)):
    need_any(admin, "users")
    rows = [[u["telegram_id"], u.get("username") or "—", u.get("first_name") or "", "بله" if u.get("referred_by") else "—",
             str(u["created_at"])[:10], "مسدود" if u.get("banned") else "فعال"]
            async for u in get_db().users.find({}).sort("created_at", -1).limit(100)]
    return render(request, admin, "page.html", title="کاربران", forms=[], tables=[
        {"title": "کاربران اخیر", "columns": ["شناسه", "یوزرنیم", "نام", "دعوت‌شده", "عضویت", "وضعیت"], "rows": rows}])


# ---------------- suppliers / tickets / audit ----------------
@router.get("/suppliers", response_class=HTMLResponse)
async def suppliers_page(request: Request, admin=Depends(auth.current_admin)):
    need_any(admin, "suppliers")
    db = get_db()
    rows = [[s["key"], s["name"], s["type"], "فعال" if s.get("enabled") else "غیرفعال"] async for s in db.suppliers.find({})]
    errs = [[str(e["updated_at"])[:16], e["order_id"][:8], e["status"], e.get("message", "")]
            async for e in db.supplier_orders.find({"status": "FAILED"}).sort("updated_at", -1).limit(30)]
    form = {"action": "/suppliers/save", "title": "افزودن تأمین‌کننده", "submit": "ذخیره", "fields": [
        form_field("key", "کلید (انگلیسی)", required=True), form_field("name", "نام", required=True),
        form_field("type", "نوع", type_="select", options=[("manual", "دستی"), ("api", "API (نیازمند پیاده‌سازی)"), ("mock", "Mock (فقط توسعه)")]),
        form_field("enabled", "فعال", type_="select", options=[("1", "بله"), ("0", "خیر")])]}
    return render(request, admin, "page.html", title="تأمین‌کنندگان", forms=[form], tables=[
        {"title": "تأمین‌کنندگان", "columns": ["کلید", "نام", "نوع", "وضعیت"], "rows": rows},
        {"title": "خطاهای تأمین‌کننده", "columns": ["زمان", "سفارش", "وضعیت", "پیام"], "rows": errs}])


@router.post("/suppliers/save")
async def suppliers_save(request: Request, admin=Depends(auth.current_admin), csrf: str = Form(""), key: str = Form(...),
                         name: str = Form(...), type: str = Form("manual"), enabled: str = Form("1")):
    auth.check_csrf(request, csrf)
    need_any(admin, "suppliers")
    await get_db().suppliers.update_one({"key": key.strip()}, {"$set": {"name": name.strip(), "type": type, "enabled": enabled == "1"}}, upsert=True)
    await audit(admin["username"], "supplier_save", key)
    return back(request, "/suppliers", "ذخیره شد.")


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
