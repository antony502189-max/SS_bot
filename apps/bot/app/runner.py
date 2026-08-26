import asyncio
import logging
import os

from aiogram import Dispatcher
from aiogram.exceptions import TelegramNetworkError
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application
from aiohttp import web

from apps.api.app.config import get_settings
from apps.api.app.telegram_bot import build_telegram_bot
from apps.bot.app.admin_panel_v2 import router as admin_panel_router
from apps.bot.app.event_management import router as event_management_router
from apps.bot.app.main import router as main_router

logger = logging.getLogger(__name__)


async def run() -> None:
    settings = get_settings()
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    if not token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is required")

    dispatcher = Dispatcher()
    # Native extension routers go first so they own enhanced administration/event
    # cards. The main router continues to handle the remaining product workflows.
    dispatcher.include_router(admin_panel_router)
    dispatcher.include_router(event_management_router)
    dispatcher.include_router(main_router)

    allowed_updates = ["message", "callback_query", "chat_member"]
    if settings.telegram_webhook_url:
        bot = build_telegram_bot(token)
        path = settings.telegram_webhook_path
        if not path.startswith("/"):
            path = f"/{path}"
        await bot.set_webhook(
            url=f"{settings.telegram_webhook_url.rstrip('/')}{path}",
            secret_token=settings.telegram_webhook_secret or None,
            allowed_updates=allowed_updates,
        )
        application = web.Application()
        SimpleRequestHandler(
            dispatcher=dispatcher,
            bot=bot,
            secret_token=settings.telegram_webhook_secret or None,
        ).register(application, path=path)
        setup_application(application, dispatcher, bot=bot)
        await web._run_app(application, host="0.0.0.0", port=8081)
        return

    retry_seconds = 5
    while True:
        bot = build_telegram_bot(token)
        try:
            await bot.delete_webhook(drop_pending_updates=False)
            await dispatcher.start_polling(
                bot,
                allowed_updates=allowed_updates,
                close_bot_session=False,
            )
            return
        except TelegramNetworkError as exc:
            logger.warning(
                "Telegram temporarily unavailable; retrying in %s seconds: %s",
                retry_seconds,
                exc,
            )
        finally:
            await bot.session.close()
        await asyncio.sleep(retry_seconds)


if __name__ == "__main__":
    asyncio.run(run())
