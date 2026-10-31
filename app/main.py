"""Single process: FastAPI (admin dashboard + Telegram webhook) + bot + background loops. Runs on Railway."""
from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from aiogram.types import Update
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from app import background
from app.bot.setup import build_bot, build_dispatcher
from app.config import get_config
from app.db import close_db, get_db, init_db
from app.services import notifier
from app.web import auth
from app.web.routes import router

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("app")
cfg = get_config()


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db(cfg.mongodb_uri, cfg.mongodb_db)
    await auth.bootstrap_owner(cfg.admin_bootstrap_username, cfg.admin_bootstrap_password)
    tasks: list[asyncio.Task] = []
    app.state.bot = None
    if cfg.bot_token:
        bot = build_bot(cfg.bot_token)
        dp = build_dispatcher()
        app.state.bot, app.state.dp = bot, dp
        notifier.set_bot(bot)
        if cfg.base_url:
            await bot.set_webhook(
                f"{cfg.base_url}/telegram/webhook", secret_token=cfg.webhook_secret or None,
                allowed_updates=dp.resolve_used_update_types(), drop_pending_updates=False)
            log.info("webhook set")
        else:
            await bot.delete_webhook()
            tasks.append(asyncio.create_task(dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())))
            log.info("long polling started (no PUBLIC_BASE_URL)")
    else:
        log.warning("BOT_TOKEN missing: bot disabled, dashboard only")
    tasks += background.start_all()
    try:
        yield
    finally:
        for t in tasks:
            t.cancel()
        if app.state.bot:
            await app.state.bot.session.close()
        await close_db()


app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
if cfg.is_production and (len(cfg.session_secret) < 32 or not cfg.webhook_secret):
    raise RuntimeError("SESSION_SECRET (>=32 chars) and WEBHOOK_SECRET are required in production")
app.add_middleware(SessionMiddleware, secret_key=cfg.session_secret or "dev-only-secret-change-me",
                   https_only=cfg.is_production, same_site="strict", max_age=8 * 3600)


@app.middleware("http")
async def security_headers(request: Request, call_next):
    resp = await call_next(request)
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["X-Frame-Options"] = "DENY"
    resp.headers["Referrer-Policy"] = "same-origin"
    resp.headers["Content-Security-Policy"] = (
        "default-src 'self'; script-src 'self' https://cdnjs.cloudflare.com; style-src 'self'; img-src 'self' data:")
    if cfg.is_production:
        resp.headers["Strict-Transport-Security"] = "max-age=31536000"
    return resp


app.mount("/static", StaticFiles(directory=str(Path(__file__).parent / "web" / "static")), name="static")


@app.get("/health")
async def health():
    try:
        await get_db().command("ping")
        return {"status": "ok"}
    except Exception:
        raise HTTPException(503, "db unavailable")


@app.post("/telegram/webhook")
async def telegram_webhook(request: Request, x_telegram_bot_api_secret_token: str | None = Header(default=None)):
    if cfg.webhook_secret and x_telegram_bot_api_secret_token != cfg.webhook_secret:
        raise HTTPException(403)
    bot, dp = request.app.state.bot, request.app.state.dp
    update = Update.model_validate(await request.json(), context={"bot": bot})
    await dp.feed_update(bot, update)
    return {"ok": True}


app.include_router(router)
