import asyncio
import os
from datetime import UTC, datetime

from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import ChatMemberUpdated, Message
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application
from aiohttp import web
from sqlalchemy import select

from apps.api.app.config import get_settings
from apps.api.app.db import SessionLocal
from apps.api.app.models import MembershipState, Role, TaskChat, TaskChatMember, User, UserStatus
from apps.api.app.services import normalize_full_name

router = Router()


class Registration(StatesGroup):
    full_name = State()


async def sync_user(message: Message) -> User:
    telegram_user = message.from_user
    assert telegram_user
    async with SessionLocal() as session:
        user = await session.scalar(select(User).where(User.telegram_id == telegram_user.id))
        if not user:
            user = User(
                telegram_id=telegram_user.id,
                telegram_username=telegram_user.username,
                role=(
                    Role.ADMIN
                    if telegram_user.id in get_settings().bootstrap_admin_ids
                    else Role.PARTICIPANT
                ),
            )
            session.add(user)
        else:
            user.telegram_username = telegram_user.username
            if user.status == UserStatus.NEEDS_USERNAME and telegram_user.username:
                user.status = UserStatus.ACTIVE
        user.last_seen_at = datetime.now(UTC)
        await session.commit()
        await session.refresh(user)
        return user


@router.message(CommandStart())
async def start(message: Message, state: FSMContext) -> None:
    user = await sync_user(message)
    if user.status == UserStatus.NEEDS_USERNAME:
        await message.answer(
            "Set a Telegram username, then send /start again to activate your profile."
        )
        return
    if user.status != UserStatus.ACTIVE:
        await state.set_state(Registration.full_name)
        await message.answer("Welcome. Please send your full name to complete registration.")
        return
    await message.answer("You are registered. Open the Mini App to see your tasks.")


@router.message(Registration.full_name, F.text)
async def receive_full_name(message: Message, state: FSMContext) -> None:
    full_name = message.text.strip()
    if len(full_name.split()) < 2 or len(full_name) > 200:
        await message.answer("Please send your first and last name.")
        return
    user = await sync_user(message)
    async with SessionLocal() as session:
        db_user = await session.get(User, user.id)
        assert db_user
        db_user.full_name = full_name
        db_user.normalized_full_name = normalize_full_name(full_name)
        db_user.status = (
            UserStatus.ACTIVE if db_user.telegram_username else UserStatus.NEEDS_USERNAME
        )
        await session.commit()
    await state.clear()
    if db_user.status == UserStatus.NEEDS_USERNAME:
        await message.answer("Full name saved. Set a Telegram username, then send /start again.")
    else:
        await message.answer("Registration complete. Your profile is ready.")


@router.chat_member()
async def membership_changed(update: ChatMemberUpdated) -> None:
    if update.new_chat_member.status not in {"member", "administrator", "creator", "owner"}:
        return
    async with SessionLocal() as session:
        task_chat = await session.scalar(
            select(TaskChat).where(TaskChat.telegram_chat_id == update.chat.id)
        )
        if not task_chat:
            return
        user = await session.scalar(
            select(User).where(User.telegram_id == update.new_chat_member.user.id)
        )
        if not user:
            return
        member = await session.scalar(
            select(TaskChatMember).where(
                TaskChatMember.task_chat_id == task_chat.id, TaskChatMember.user_id == user.id
            )
        )
        if member:
            member.state = MembershipState.JOINED
            member.next_reminder_at = None
            member.last_error = None
            await session.commit()


async def run() -> None:
    settings = get_settings()
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    if not token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is required")
    bot = Bot(token)
    dispatcher = Dispatcher()
    dispatcher.include_router(router)
    if settings.telegram_webhook_url:
        path = settings.telegram_webhook_path
        if not path.startswith("/"):
            path = f"/{path}"
        await bot.set_webhook(
            url=f"{settings.telegram_webhook_url.rstrip('/')}{path}",
            secret_token=settings.telegram_webhook_secret or None,
            allowed_updates=["message", "chat_member"],
        )
        application = web.Application()
        SimpleRequestHandler(
            dispatcher=dispatcher, bot=bot, secret_token=settings.telegram_webhook_secret or None
        ).register(application, path=path)
        setup_application(application, dispatcher, bot=bot)
        await web._run_app(application, host="0.0.0.0", port=8081)
        return
    await bot.delete_webhook(drop_pending_updates=False)
    await dispatcher.start_polling(bot, allowed_updates=["message", "chat_member"])


if __name__ == "__main__":
    asyncio.run(run())
