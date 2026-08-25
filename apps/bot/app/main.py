import asyncio
import os
from datetime import UTC, datetime

from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import ChatMemberUpdated, Message
from sqlalchemy import select

from apps.api.app.db import SessionLocal
from apps.api.app.models import MembershipState, TaskChat, TaskChatMember, User, UserStatus
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
            user = User(telegram_id=telegram_user.id, telegram_username=telegram_user.username)
            session.add(user)
        else:
            user.telegram_username = telegram_user.username
        user.last_seen_at = datetime.now(UTC)
        await session.commit()
        await session.refresh(user)
        return user


@router.message(CommandStart())
async def start(message: Message, state: FSMContext) -> None:
    user = await sync_user(message)
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
        db_user.status = UserStatus.ACTIVE
        await session.commit()
    await state.clear()
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
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    if not token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is required")
    bot = Bot(token)
    dispatcher = Dispatcher()
    dispatcher.include_router(router)
    await dispatcher.start_polling(bot, allowed_updates=["message", "chat_member"])


if __name__ == "__main__":
    asyncio.run(run())
