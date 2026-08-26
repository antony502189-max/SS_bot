import asyncio
import os
import uuid
from datetime import UTC, datetime

from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    ChatMemberUpdated,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
)
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application
from aiohttp import web
from sqlalchemy import or_, select

from apps.api.app.config import get_settings
from apps.api.app.db import SessionLocal
from apps.api.app.models import (
    MembershipState,
    Role,
    Task,
    TaskChat,
    TaskChatMember,
    TaskKind,
    TaskMember,
    TaskStatus,
    User,
    UserStatus,
)
from apps.api.app.schemas import TaskCreate
from apps.api.app.services import create_task, normalize_full_name

router = Router()


class Registration(StatesGroup):
    full_name = State()


class TaskIssue(StatesGroup):
    title = State()
    kind = State()
    people = State()
    deadline = State()
    cleanup = State()


class AdminGrant(StatesGroup):
    username = State()


menu_keyboard = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="📋 Мои задачи"), KeyboardButton(text="👤 Профиль")],
        [KeyboardButton(text="➕ Выдать задачу")],
        [KeyboardButton(text="👑 Назначить администратора")],
        [KeyboardButton(text="ℹ️ Помощь")],
    ],
    resize_keyboard=True,
    input_field_placeholder="Выберите действие",
)


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
            "Укажите имя пользователя в настройках Telegram, затем снова отправьте /start, "
            "чтобы активировать профиль."
        )
        return
    if user.status != UserStatus.ACTIVE:
        await state.set_state(Registration.full_name)
        await message.answer("Добро пожаловать! Отправьте ваше имя и фамилию для регистрации.")
        return
    await message.answer(
        "Вы уже зарегистрированы. Выберите действие в меню.", reply_markup=menu_keyboard
    )


@router.message(Registration.full_name, F.text)
async def receive_full_name(message: Message, state: FSMContext) -> None:
    full_name = message.text.strip()
    if len(full_name.split()) < 2 or len(full_name) > 200:
        await message.answer("Пожалуйста, отправьте имя и фамилию.")
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
        await message.answer(
            "Имя сохранено. Укажите имя пользователя в настройках Telegram, затем снова "
            "отправьте /start."
        )
    else:
        await message.answer(
            "Регистрация завершена. Ваш профиль готов к работе.", reply_markup=menu_keyboard
        )


@router.message(F.text == "📋 Мои задачи")
async def my_tasks(message: Message) -> None:
    user = await sync_user(message)
    async with SessionLocal() as session:
        tasks = list(
            (
                await session.scalars(
                    select(Task)
                    .join(TaskMember)
                    .where(
                        TaskMember.user_id == user.id,
                        Task.status.in_(
                            [TaskStatus.ACTIVE, TaskStatus.RETURNED, TaskStatus.OVERDUE]
                        ),
                    )
                    .order_by(Task.deadline)
                    .limit(10)
                )
            ).all()
        )
    if not tasks:
        await message.answer("Сейчас у вас нет активных задач.", reply_markup=menu_keyboard)
        return
    labels = {
        TaskStatus.ACTIVE: "активна",
        TaskStatus.RETURNED: "на доработке",
        TaskStatus.OVERDUE: "просрочена",
    }
    lines = ["Ваши активные задачи:"]
    for task in tasks:
        deadline = task.deadline.astimezone(UTC).strftime("%d.%m %H:%M")
        lines.append(f"• {task.title} — {labels[task.status]}, до {deadline}")
    await message.answer("\n".join(lines), reply_markup=menu_keyboard)


@router.message(F.text == "👤 Профиль")
async def profile(message: Message) -> None:
    user = await sync_user(message)
    username = f"@{user.telegram_username}" if user.telegram_username else "не указан"
    await message.answer(
        f"Профиль:\nИмя: {user.full_name or 'не заполнено'}\n"
        f"Имя пользователя: {username}",
        reply_markup=menu_keyboard,
    )


@router.message(F.text == "ℹ️ Помощь")
async def help_message(message: Message) -> None:
    await message.answer(
        "Нажмите «Мои задачи», чтобы получить список текущих задач. "
        "В Mini App можно выполнять задачи, прикладывать отчёты и фотографии.",
        reply_markup=menu_keyboard,
    )


@router.message(F.text == "👑 Назначить администратора")
async def admin_grant_start(message: Message, state: FSMContext) -> None:
    user = await sync_user(message)
    if user.telegram_id not in get_settings().superadmin_ids:
        await message.answer("Назначать администраторов может только главный администратор.")
        return
    await state.set_state(AdminGrant.username)
    await message.answer("Введите @username зарегистрированного пользователя.")


@router.message(AdminGrant.username, F.text)
async def admin_grant_finish(message: Message, state: FSMContext) -> None:
    username = message.text.strip().lstrip("@")
    async with SessionLocal() as session:
        target = await session.scalar(select(User).where(User.telegram_username == username))
        if not target or target.status != UserStatus.ACTIVE:
            await message.answer("Активный пользователь не найден. Повторите ввод.")
            return
        target.role = Role.ADMIN
        await session.commit()
    await state.clear()
    await message.answer(f"@{username} назначен администратором задач.", reply_markup=menu_keyboard)


@router.message(F.text == "➕ Выдать задачу")
async def issue_task_start(message: Message, state: FSMContext) -> None:
    user = await sync_user(message)
    if user.role not in {Role.ADMIN, Role.SECTOR_HEAD}:
        await message.answer("Выдавать задачи могут только администраторы и руководители секторов.")
        return
    await state.set_state(TaskIssue.title)
    await message.answer("Введите название задачи.")


@router.message(TaskIssue.title, F.text)
async def issue_task_title(message: Message, state: FSMContext) -> None:
    title = message.text.strip()
    if len(title) < 2 or len(title) > 200:
        await message.answer("Название должно содержать от 2 до 200 символов. Повторите ввод.")
        return
    await state.update_data(title=title)
    await state.set_state(TaskIssue.kind)
    await message.answer(
        "Выберите тип задачи.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="👤 Личная", callback_data="issue:kind:individual"),
            InlineKeyboardButton(text="👥 Групповая", callback_data="issue:kind:group"),
        ]]),
    )


@router.callback_query(TaskIssue.kind, F.data.startswith("issue:kind:"))
async def issue_task_kind(callback: CallbackQuery, state: FSMContext) -> None:
    kind = callback.data.rsplit(":", 1)[-1]
    await state.update_data(kind=kind, member_ids=[])
    await state.set_state(TaskIssue.people)
    await callback.answer()
    await callback.message.answer(
        "Введите часть имени или @username. Результаты появятся кнопками."
    )


def people_keyboard(users: list[User], selected: set[str]) -> InlineKeyboardMarkup:
    rows = []
    for user in users:
        label = user.full_name or f"@{user.telegram_username or ''}"
        prefix = "✅" if str(user.id) in selected else "➕"
        rows.append(
            [InlineKeyboardButton(text=f"{prefix} {label}", callback_data=f"issue:person:{user.id}")]
        )
    rows.append(
        [InlineKeyboardButton(text=f"Готово ({len(selected)})", callback_data="issue:people:done")]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


@router.message(TaskIssue.people, F.text)
async def issue_task_people(message: Message, state: FSMContext) -> None:
    query = message.text.strip().lstrip("@")
    if len(query) < 2:
        await message.answer("Введите минимум две буквы имени или username.")
        return
    data = await state.get_data()
    async with SessionLocal() as session:
        statement = (
            select(User)
            .where(
                User.status == UserStatus.ACTIVE,
                or_(
                    User.telegram_username.ilike(f"%{query}%"),
                    User.normalized_full_name.ilike(f"%{normalize_full_name(query)}%"),
                ),
            )
            .order_by(User.full_name)
            .limit(8)
        )
        users = list((await session.scalars(statement)).all())
    if not users:
        await message.answer("Никого не нашёл. Измените запрос.")
        return
    await message.answer(
        "Выберите людей:", reply_markup=people_keyboard(users, set(data["member_ids"]))
    )


@router.callback_query(TaskIssue.people, F.data.startswith("issue:person:"))
async def issue_task_person(callback: CallbackQuery, state: FSMContext) -> None:
    person_id = callback.data.rsplit(":", 1)[-1]
    data = await state.get_data()
    selected = set(data["member_ids"])
    selected.symmetric_difference_update({person_id})
    await state.update_data(member_ids=list(selected))
    await callback.answer("Добавлен" if person_id in selected else "Убран")


@router.callback_query(TaskIssue.people, F.data == "issue:people:done")
async def issue_task_people_done(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    count = len(data["member_ids"])
    if not count or (data["kind"] == "individual" and count != 1):
        await callback.answer(
            "Для личной задачи выберите одного, для групповой — одного или больше.",
            show_alert=True,
        )
        return
    await state.set_state(TaskIssue.deadline)
    await callback.message.answer("Введите срок задачи: ДД.ММ.ГГГГ ЧЧ:ММ.")
    await callback.answer()


@router.message(TaskIssue.deadline, F.text)
async def issue_task_deadline(message: Message, state: FSMContext) -> None:
    try:
        deadline = datetime.strptime(message.text.strip(), "%d.%m.%Y %H:%M").replace(tzinfo=UTC)
    except ValueError:
        await message.answer("Неверный формат. Используйте ДД.ММ.ГГГГ ЧЧ:ММ.")
        return
    await state.update_data(deadline=deadline.isoformat())
    await state.set_state(TaskIssue.cleanup)
    await message.answer("Введите время удаления рабочей группы: ДД.ММ.ГГГГ ЧЧ:ММ.")


@router.message(TaskIssue.cleanup, F.text)
async def issue_task_cleanup(message: Message, state: FSMContext) -> None:
    try:
        cleanup_at = datetime.strptime(message.text.strip(), "%d.%m.%Y %H:%M").replace(tzinfo=UTC)
    except ValueError:
        await message.answer("Неверный формат. Используйте ДД.ММ.ГГГГ ЧЧ:ММ.")
        return
    data = await state.get_data()
    actor = await sync_user(message)
    try:
        async with SessionLocal() as session:
            db_actor = await session.get(User, actor.id)
            assert db_actor
            task, _ = await create_task(
                session,
                db_actor,
                TaskCreate(
                    title=data["title"],
                    deadline=datetime.fromisoformat(data["deadline"]),
                    cleanup_at=cleanup_at if data["kind"] == "group" else None,
                    kind=TaskKind(data["kind"]),
                    leader_id=actor.id if data["kind"] == "group" else None,
                    member_ids=[uuid.UUID(value) for value in data["member_ids"]],
                ),
                str(uuid.uuid4()),
            )
            await session.commit()
    except Exception:
        await message.answer("Не удалось создать задачу. Проверьте срок и права доступа.")
        await state.clear()
        return
    await state.clear()
    await message.answer(
        f"Задача «{task.title}» создана. Исполнители увидят её в разделе «Мои задачи».",
        reply_markup=menu_keyboard,
    )


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
