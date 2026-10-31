"""Admin dashboard through the real FastAPI routes (ASGI, mock DB, fake bot)."""
import re

import httpx
import pytest
from fastapi import FastAPI
from starlette.middleware.sessions import SessionMiddleware

import auth
from conftest import make_gift
from constants import OrderStatus as S
from db import get_db
from payment_manager import get_provider
import fulfillment, orders
from web_routes import router


@pytest.fixture
async def client(tg):
    app = FastAPI()
    app.add_middleware(SessionMiddleware, secret_key="x" * 40, https_only=False)
    app.include_router(router)
    await auth.bootstrap_owner("owner", "long-test-password")
    c = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t")
    r = await c.get("/login")
    await c.post("/login", data={"username": "owner", "password": "long-test-password", "csrf": _csrf(r.text)})
    yield c
    await c.aclose()


def _csrf(html):
    return re.search(r'name="csrf" value="([^"]+)"', html).group(1)


async def _post(c, page, path, **data):
    tok = _csrf((await c.get(page)).text)
    return await c.post(path, data={"csrf": tok, **data}, follow_redirects=True)


def _form(**over):
    base = dict(name="N", description="", category_id="", kind="stars", amount="100", gift_id="", delivery_mode="manual",
                price_source="manual", manual_amount="", manual_currency="IRT", profit_toman="", stock="", enabled="1")
    base.update(over)
    return base


async def test_pages_render(client):
    for path in ("/", "/products", "/pricing", "/orders", "/suppliers", "/payments", "/categories"):
        r = await client.get(path)
        assert r.status_code == 200, path
    assert "Telegram Stars" in (await client.get("/products")).text


async def test_create_stars_product_defaults(client):
    await _post(client, "/products", "/products/new", **_form(name="500 Stars", amount="500"))
    p = await get_db().products.find_one({"name": "500 Stars"})
    assert p["delivery"] == "manual" and p["supplier_key"] == "manual" and p["stock"] is None
    assert p["pricing"] == {"source": "manual", "manual_amount": "500", "manual_currency": "XTR"}


async def test_premium_defaults_to_telegram_price_and_auto_delivery(client):
    await _post(client, "/products", "/products/new", **_form(name="P6", kind="premium", amount="6", delivery_mode="auto"))
    p = await get_db().products.find_one({"name": "P6"})
    assert p["delivery"] == "auto_premium" and p["supplier_key"] == "telegram" and p["pricing"] == {"source": "telegram"}


@pytest.mark.parametrize("bad", [dict(kind="premium", amount="5"), dict(profit_toman="7001"), dict(stock="-1"),
                                 dict(kind="gift", gift_id=""), dict(amount="")])
async def test_invalid_product_is_rejected(client, bad):
    before = await get_db().products.count_documents({})
    await _post(client, "/products", "/products/new", **_form(name="BAD", **bad))
    assert await get_db().products.count_documents({}) == before


async def test_edit_keeps_zero_stock_and_toggle(client, tg):
    await _post(client, "/products", f"/products/{tg.stars}/save", **_form(name="100 Stars", stock="0"))
    assert (await get_db().products.find_one({"public_id": tg.stars}))["stock"] == 0
    page = (await client.get(f"/products/{tg.stars}")).text
    assert 'name="stock" value="0"' in page             # 0 must not turn into "unlimited" on re-save
    await _post(client, "/products", f"/products/{tg.stars}/toggle")
    assert (await get_db().products.find_one({"public_id": tg.stars}))["enabled"] is False


async def test_import_gifts_button(client, tg):
    tg.bot.gifts = [make_gift("g1", 50), make_gift("gNew", 10)]
    await _post(client, "/products", "/products/import-gifts")
    assert (await get_db().products.find_one({"gift_id": "gNew"}))["enabled"] is False


async def test_manual_rate_and_settings(client, tg):
    await _post(client, "/pricing", "/pricing/manual-rate", rate="120,000")
    assert (await get_db().exchange_rates.find_one({"_id": "USD"}))["toman_per_unit"] == "120000"
    await _post(client, "/pricing", "/pricing/settings", profit_toman="7001")
    assert (await get_db().settings.find_one({"key": "profit_toman"}))["value"] == "5000"      # rejected
    await _post(client, "/pricing", "/pricing/settings", auto_refresh_enabled="false", auto_delivery_enabled="false")
    assert (await get_db().settings.find_one({"key": "auto_refresh_enabled"}))["value"] == "false"
    await _post(client, "/pricing", "/pricing/manual-rate", rate="abc")
    assert (await get_db().exchange_rates.find_one({"_id": "USD"}))["toman_per_unit"] == "120000"


async def test_manual_orders_filter_and_delivery(client, tg, sent):
    stars = get_provider("stars")
    o = await orders.create_order(42, tg.stars, 1)
    res = await stars.handle_successful_payment(42, o["public_id"], "XTR", o["stars_amount"], "c1")
    await fulfillment.on_order_paid(res.order)
    page = (await client.get("/orders?status=MANUAL")).text
    assert o["public_id"][:8] in page and "تحویل شد" in page
    await _post(client, "/orders", f"/orders/{o['public_id']}/deliver", delivery_text="ok")
    assert (await orders.get_order(o["public_id"]))["status"] == S.FULFILLED
    assert o["public_id"][:8] not in (await client.get("/orders?status=MANUAL")).text


async def test_refund_button(client, tg, sent):
    stars = get_provider("stars")
    o = await orders.create_order(43, tg.stars, 1)
    res = await stars.handle_successful_payment(43, o["public_id"], "XTR", o["stars_amount"], "c2")
    await fulfillment.on_order_paid(res.order)
    await _post(client, "/orders", f"/orders/{o['public_id']}/refund")
    assert (await orders.get_order(o["public_id"]))["status"] == S.REFUNDED and tg.bot.refunds


async def test_requires_login_and_csrf(tg):
    app = FastAPI()
    app.add_middleware(SessionMiddleware, secret_key="x" * 40, https_only=False)
    app.include_router(router)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        assert (await c.get("/products")).status_code == 303
        assert (await c.post("/products/new", data=_form())).status_code in (303, 403)
