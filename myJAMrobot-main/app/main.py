from __future__ import annotations

import asyncio
import hmac
import json
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.types import Update
from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse
from sqlalchemy import text

from app.logging_safety import configure_safe_logging

configure_safe_logging()

from app.bot.canvas import router as canvas_router
from app.bot.lyrics import router as lyrics_router
from app.bot.ops_control import install_operational_control_middleware, router as onoff_router
from app.bot.radio import router as radio_router
from app.bot.setup_commands import setup_bot_commands
from app.bot.story import router as story_router
from app.bot.telegram import _register_handlers, bot_dispatcher, shutdown_telegram_bot
from app.config.settings import (
    BASE_URL,
    MAX_WEBHOOK_BYTES,
    TELEGRAM_BOT_TOKEN,
    telegram_webhook_secret,
    validate_required_env,
)
from app.db.database import engine, init_db
from app.security.rate_limit import rate_limit_status

logger = logging.getLogger(__name__)
app = FastAPI(
    title="myJAMrobot",
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)
dispatcher: Dispatcher = bot_dispatcher
bot: Bot | None = None
_configured = False
_startup_task: asyncio.Task[None] | None = None
_telegram_status = "pending"
_database_status = "pending"


def _configure_dispatcher() -> None:
    global _configured
    if _configured:
        return
    install_operational_control_middleware(dispatcher)
    _register_handlers(dispatcher)
    dispatcher.include_router(onoff_router)
    dispatcher.include_router(canvas_router)
    dispatcher.include_router(story_router)
    dispatcher.include_router(radio_router)
    dispatcher.include_router(lyrics_router)
    _configured = True


async def _configure_telegram() -> None:
    global bot, _telegram_status
    _telegram_status = "starting"
    local_bot: Bot | None = None
    try:
        _configure_dispatcher()
        local_bot = Bot(
            token=TELEGRAM_BOT_TOKEN,
            default=DefaultBotProperties(parse_mode=ParseMode.HTML),
        )
        await local_bot.set_webhook(
            f"{BASE_URL}/webhook",
            allowed_updates=dispatcher.resolve_used_update_types(),
            secret_token=telegram_webhook_secret(),
        )
        await setup_bot_commands(local_bot)
        bot = local_bot
        _telegram_status = "ready"
        logger.info("TELEGRAM_READY commands=9")
    except Exception:
        _telegram_status = "failed"
        logger.exception("TELEGRAM_STARTUP_FAILED")
        if local_bot is not None:
            await local_bot.session.close()


def _initialize_database() -> None:
    global _database_status
    _database_status = "starting"
    try:
        init_db()
        with engine.begin() as connection:
            connection.execute(text("SELECT 1"))
        _database_status = "ready"
    except Exception:
        _database_status = "failed"
        logger.exception("DATABASE_STARTUP_FAILED")


@app.on_event("startup")
async def startup() -> None:
    global _startup_task, _telegram_status
    missing = validate_required_env()
    if missing:
        logger.warning("STARTUP_CONFIGURATION_INCOMPLETE names=%s", ",".join(missing))
    _initialize_database()
    if missing:
        _telegram_status = "configuration_incomplete"
        return
    _startup_task = asyncio.create_task(_configure_telegram(), name="telegram-startup")


@app.on_event("shutdown")
async def shutdown() -> None:
    global bot, _startup_task
    if _startup_task is not None and not _startup_task.done():
        _startup_task.cancel()
        await asyncio.gather(_startup_task, return_exceptions=True)
    await shutdown_telegram_bot()
    if bot is not None:
        await bot.session.close()
        bot = None


@app.get("/healthz")
def healthz() -> dict[str, object]:
    return {
        "status": "ok",
        "commands": 9,
        "rate_limit": rate_limit_status(),
    }


@app.get("/readyz")
def readyz() -> JSONResponse:
    ready = _database_status == "ready" and _telegram_status == "ready" and bot is not None and _configured
    return JSONResponse(
        {
            "status": "ready" if ready else "not_ready",
            "database": _database_status,
            "telegram": _telegram_status,
        },
        status_code=200 if ready else 503,
    )


@app.post("/webhook")
async def telegram_webhook(request: Request) -> object:
    if bot is None or not _configured:
        return Response(status_code=503)
    expected = telegram_webhook_secret()
    provided = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
    if not expected or not hmac.compare_digest(provided, expected):
        return Response(status_code=403)
    content_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    if content_type != "application/json":
        return Response(status_code=415)
    try:
        declared = int(request.headers.get("content-length") or 0)
    except ValueError:
        return Response(status_code=400)
    if declared < 0:
        return Response(status_code=400)
    if declared > MAX_WEBHOOK_BYTES:
        return Response(status_code=413)
    chunks: list[bytes] = []
    received = 0
    async for chunk in request.stream():
        received += len(chunk)
        if received > MAX_WEBHOOK_BYTES:
            return Response(status_code=413)
        chunks.append(chunk)
    body = b"".join(chunks)
    if not body:
        return Response(status_code=400)
    try:
        payload = json.loads(body)
        if not isinstance(payload, dict):
            raise ValueError("update must be an object")
        update = Update.model_validate(payload, context={"bot": bot})
        await dispatcher.feed_update(bot, update)
    except (json.JSONDecodeError, ValueError):
        return Response(status_code=400)
    except Exception:
        logger.exception("WEBHOOK_DISPATCH_FAILED")
        return Response(status_code=500)
    return {"ok": True}
