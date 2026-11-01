"""Public Zarinpal return page + admin pages (ASGI, mock DB, FAKE gateway)."""
import re

import httpx
import pytest
from fastapi import FastAPI
from starlette.middleware.sessions import SessionMiddleware

import auth, topups, wallet
from db import get_db
from test_topups import FakeGateway, zp  # noqa: F401  (fixtures)
from web_pay import router as pay_router
from web_routes import router


@pytest.fixture
async def pay_client(db, monkeypatch, zp):
    monkeypatch.setattr(topups, "get_client", lambda: zp)
    app = FastAPI()
    app.include_router(pay_router)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        yield c


async def test_callback_page_credits_once_after_server_verification(pay_client, zp):
    row = await topups.create_topup(7, 100_000, client=zp)
    authority = zp.requests[-1][0]
    zp.paid[authority] = 1_000_000
    url = f"/pay/zarinpal/callback?t={row['public_id']}&Authority={authority}&Status=OK"
    r1, r2 = await pay_client.get(url), await pay_client.get(url)
    assert r1.status_code == 200 and "تأیید شد" in r1.text and r2.status_code == 200
    assert await wallet.get_balance(7) == 100_000


async def test_callback_with_status_ok_but_no_payment_credits_nothing(pay_client, zp):
    row = await topups.create_topup(7, 100_000, client=zp)
    authority = zp.requests[-1][0]
    r = await pay_client.get(f"/pay/zarinpal/callback?t={row['public_id']}&Authority={authority}&Status=OK")
    assert "تأیید نشد" in r.text and await wallet.get_balance(7) == 0


async def test_callback_nok_and_garbage(pay_client, zp):
    row = await topups.create_topup(7, 100_000, client=zp)
    authority = zp.requests[-1][0]
    assert "لغو" in (await pay_client.get(f"/pay/zarinpal/callback?t={row['public_id']}&Authority={authority}&Status=NOK")).text
    assert (await pay_client.get("/pay/zarinpal/callback?t=../x&Authority=a&Status=OK")).status_code == 400
    assert (await pay_client.get("/pay/zarinpal/callback")).status_code == 400
    assert await wallet.get_balance(7) == 0


@pytest.fixture
async def admin_client(db, sent):
    app = FastAPI()
    app.add_middleware(SessionMiddleware, secret_key="x" * 40, https_only=False)
    app.include_router(router)
    await auth.bootstrap_owner("owner", "long-test-password")
    c = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t")
    r = await c.get("/login")
    tok = re.search(r'name="csrf" value="([^"]+)"', r.text).group(1)
    await c.post("/login", data={"username": "owner", "password": "long-test-password", "csrf": tok})
    yield c
    await c.aclose()


async def test_admin_topups_and_wallet_pages_and_settings(admin_client):
    for path in ("/topups", "/wallets", "/wallets?user=5"):
        assert (await admin_client.get(path)).status_code == 200, path
    tok = re.search(r'name="csrf" value="([^"]+)"', (await admin_client.get("/topups")).text).group(1)
    await admin_client.post("/topups/settings", data={"csrf": tok, "topup_min_toman": "20000", "topup_presets": "30000, 60000",
                                                      "custom_stars_enabled": "true"}, follow_redirects=True)
    import settings as st
    assert await st.get_setting("topup_min_toman") == "20000" and await st.get_setting("topup_presets") == "30000,60000"
    assert await st.get_bool("custom_stars_enabled")
    bad = await admin_client.post("/topups/settings", data={"csrf": tok, "topup_min_toman": "9000000"}, follow_redirects=True)
    assert await st.get_setting("topup_min_toman") == "20000"      # min > max is refused
