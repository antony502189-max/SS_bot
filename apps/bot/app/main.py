import asyncio
import logging
import os
import uuid
from datetime import UTC, datetime
from io import BytesIO
from zoneinfo import ZoneInfo

from aiogram import Dispatcher, F, Router
from aiogram.exceptions import TelegramBadRequest, TelegramNetworkError
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    BufferedInputFile,
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
from fastapi import HTTPException
from sqlalchemy import func, or_, select

from apps.api.app.archive import build_event_archive_pdf, build_photo_zip
from apps.api.app.config import get_settings
from apps.api.app.db import SessionLocal
from apps.api.app.models import (
    ChatStatus,
    Event,
    EventParticipant,
    MembershipState,
    OutboxEvent,
    ReportStatus,
    Role,
    Sector,
    Task,
    TaskChat,
    TaskChatMember,
    TaskChecklistItem,
    TaskKind,
    TaskMember,
    TaskPhoto,
    TaskReport,
    TaskStatus,
    User,
    UserStatus,
)
from apps.api.app.photos import inspect_photo
from apps.api.app.routers import admin as admin_routes
from apps.api.app.routers import events as event_routes
from apps.api.app.routers import reports as report_routes
from apps.api.app.routers import tasks as task_routes
from apps.api.app.schemas import (
    ChecklistItemCreate,
    ChecklistUpdate,
    EventCreate,
    ReportCreate,
    ReportDecision,
    RetentionExtend,
    TaskCreate,
    TaskMemberCreate,
    TaskUpdate,
    UserAdminUpdate,
)
from apps.api.app.services import create_event, create_task, normalize_full_name
from apps.api.app.storage import delete_object, get_object_bytes, put_object
from apps.api.app.telegram_bot import build_telegram_bot
from apps.telegram_user_service.app.client import legacy_mtproto_channel_id

router = Router()
USER_TIMEZONE = ZoneInfo("Europe/Minsk")
logger = logging.getLogger(__name__)

OPEN_TASK_STATUSES = {TaskStatus.ACTIVE, TaskStatus.RETURNED, TaskStatus.OVERDUE}
STATUS_LABELS = {
    TaskStatus.DRAFT: "черновик",
    TaskStatus.ACTIVE: "активна",
    TaskStatus.SUBMITTED: "на проверке",
    TaskStatus.RETURNED: "на доработке",
    TaskStatus.COMPLETED: "выполнена",
    TaskStatus.OVERDUE: "просрочена",
    TaskStatus.CANCELLED: "отменена",
}
ROLE_LABELS = {
    Role.PARTICIPANT: "Участник",
    Role.SECTOR_HEAD: "Председатель сектора",
    Role.ADMIN: "Администратор",
}
REPORT_LABELS = {
    ReportStatus.DRAFT: "черновик",
    ReportStatus.SUBMITTED: "отправлен",
    ReportStatus.RETURNED: "возвращён",
    ReportStatus.APPROVED: "принят",
}


class Registration(StatesGroup):
    full_name = State()


class TaskIssue(StatesGroup):
    title = State()
    description = State()
    event = State()
    kind = State()
    people = State()
    leader = State()
    deadline = State()
    checklist = State()


class TaskEdit(StatesGroup):
    value = State()


class TaskMemberAdd(StatesGroup):
    query = State()


class ChecklistAdd(StatesGroup):
    title = State()


class ReportFlow(StatesGroup):
    comment = State()
    photo = State()
    return_reason = State()


class EventCreateFlow(StatesGroup):
    title = State()
    starts_at = State()
    ends_at = State()
    budget = State()
    description = State()
    people = State()


class RetentionFlow(StatesGroup):
    until = State()


def main_keyboard(user: User) -> ReplyKeyboardMarkup:
    rows = [
        [KeyboardButton(text="📋 Мои задачи"), KeyboardButton(text="👤 Профиль")],
    ]
    if user.role in {Role.ADMIN, Role.SECTOR_HEAD}:
        rows.append([KeyboardButton(text="➕ Выдать задачу"), KeyboardButton(text="🗓 Мероприятия")])
    if user.role == Role.ADMIN:
        rows.append([KeyboardButton(text="⚙️ Администрирование")])
    rows.append([KeyboardButton(text="ℹ️ Помощь")])
    return ReplyKeyboardMarkup(
        keyboard=rows,
        resize_keyboard=True,
        input_field_placeholder="Выберите действие",
    )


def parse_input_datetime(value: str) -> datetime:
    """Interpret user-entered dates as Minsk local time and store them in UTC."""
    return (
        datetime.strptime(value.strip(), "%d.%m.%Y %H:%M")
        .replace(tzinfo=USER_TIMEZONE)
        .astimezone(UTC)
    )


def format_dt(value: datetime | None) -> str:
    if not value:
        return "не указано"
    return value.astimezone(USER_TIMEZONE).strftime("%d.%m.%Y %H:%M")


def display_user(user: User) -> str:
    name = user.full_name or "Без имени"
    username = f" @{user.telegram_username}" if user.telegram_username else ""
    return f"{name}{username}"[:52]


def people_keyboard(users: list[User], selected: set[str]) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for user in users:
        prefix = "✅" if str(user.id) in selected else "➕"
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"{prefix} {display_user(user)}",
                    callback_data=f"issue:person:{user.id}",
                )
            ]
        )
    rows.append(
        [InlineKeyboardButton(text=f"Готово ({len(selected)})", callback_data="issue:people:done")]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def event_people_keyboard(users: list[User], selected: set[str]) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for user in users:
        prefix = "✅" if str(user.id) in selected else "➕"
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"{prefix} {display_user(user)}",
                    callback_data=f"evp:{user.id}",
                )
            ]
        )
    rows.append(
        [InlineKeyboardButton(text=f"Создать ({len(selected)})", callback_data="evp:done")]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def sync_telegram_user(telegram_user) -> User:
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
            if user.status not in {UserStatus.INACTIVE, UserStatus.BLOCKED} and user.full_name:
                user.status = (
                    UserStatus.ACTIVE if telegram_user.username else UserStatus.NEEDS_USERNAME
                )
        user.last_seen_at = datetime.now(UTC)
        await session.commit()
        await session.refresh(user)
        return user


async def sync_user(message: Message) -> User:
    assert message.from_user
    return await sync_telegram_user(message.from_user)


async def sync_callback_user(callback: CallbackQuery) -> User:
    return await sync_telegram_user(callback.from_user)


def can_manage_task(actor: User, task: Task) -> bool:
    if actor.role == Role.ADMIN:
        return True
    return actor.role == Role.SECTOR_HEAD and actor.sector_id == task.sector_id


async def answer_http_error(target: Message | CallbackQuery, exc: Exception) -> None:
    if isinstance(exc, HTTPException):
        text = str(exc.detail)
    else:
        logger.exception("Bot operation failed", exc_info=exc)
        text = "Операция не выполнена. Попробуйте ещё раз."
    if isinstance(target, CallbackQuery):
        await target.answer(text[:180], show_alert=True)
    else:
        await target.answer(text)


async def search_users(actor: User, query: str, *, exclude_ids: set[uuid.UUID] | None = None) -> list[User]:
    cleaned = query.strip().lstrip("@")
    if len(cleaned) < 2:
        return []
    async with SessionLocal() as session:
        statement = (
            select(User)
            .where(
                User.status == UserStatus.ACTIVE,
                or_(
                    User.telegram_username.ilike(f"%{cleaned}%"),
                    User.normalized_full_name.ilike(f"%{normalize_full_name(cleaned)}%"),
                ),
            )
            .order_by(User.full_name)
            .limit(8)
        )
        if actor.role == Role.SECTOR_HEAD:
            statement = statement.where(User.sector_id == actor.sector_id)
        if exclude_ids:
            statement = statement.where(User.id.not_in(exclude_ids))
        return list((await session.scalars(statement)).all())


def task_filters_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="Открытые", callback_data="tl:open"),
                InlineKeyboardButton(text="Все", callback_data="tl:all"),
            ],
            [
                InlineKeyboardButton(text="Просроченные", callback_data="tl:overdue"),
                InlineKeyboardButton(text="Завершённые", callback_data="tl:done"),
            ],
        ]
    )


async def load_tasks_for_user(user_id: uuid.UUID, mode: str) -> list[Task]:
    async with SessionLocal() as session:
        statement = (
            select(Task)
            .join(TaskMember)
            .where(TaskMember.user_id == user_id)
            .order_by(Task.deadline.desc())
            .limit(20)
        )
        if mode == "open":
            statement = statement.where(
                Task.status.in_(
                    [
                        TaskStatus.ACTIVE,
                        TaskStatus.RETURNED,
                        TaskStatus.OVERDUE,
                        TaskStatus.SUBMITTED,
                    ]
                )
            )
        elif mode == "overdue":
            statement = statement.where(Task.status == TaskStatus.OVERDUE)
        elif mode == "done":
            statement = statement.where(
                Task.status.in_([TaskStatus.COMPLETED, TaskStatus.CANCELLED])
            )
        return list((await session.scalars(statement)).unique().all())


async def render_task_list(user: User, mode: str = "open") -> tuple[str, InlineKeyboardMarkup]:
    tasks = await load_tasks_for_user(user.id, mode)
    rows: list[list[InlineKeyboardButton]] = []
    for task in tasks:
        label = STATUS_LABELS.get(task.status, task.status.value)
        title = task.title if len(task.title) <= 34 else f"{task.title[:31]}…"
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"{title} · {label}",
                    callback_data=f"task:{task.id}",
                )
            ]
        )
    rows.extend(task_filters_keyboard().inline_keyboard)
    title = {
        "open": "📋 Открытые задачи",
        "all": "📋 Все задачи",
        "overdue": "⏰ Просроченные задачи",
        "done": "✅ Завершённые задачи",
    }.get(mode, "📋 Задачи")
    if not tasks:
        return f"{title}\n\nЗадач в этом разделе нет.", InlineKeyboardMarkup(inline_keyboard=rows)
    return f"{title}\n\nВыберите задачу:", InlineKeyboardMarkup(inline_keyboard=rows)


async def build_task_view(actor: User, task_id: uuid.UUID) -> tuple[str, InlineKeyboardMarkup]:
    async with SessionLocal() as session:
        task = await session.get(Task, task_id)
        if not task:
            raise HTTPException(status_code=404, detail="Задача не найдена")
        membership = await session.scalar(
            select(TaskMember.id).where(
                TaskMember.task_id == task.id,
                TaskMember.user_id == actor.id,
            )
        )
        if not membership:
            raise HTTPException(status_code=403, detail="Нет доступа к задаче")
        detail = await task_routes.task_detail(session, task)
        event = await session.get(Event, task.event_id) if task.event_id else None
        report = await session.scalar(select(TaskReport).where(TaskReport.task_id == task.id))
        chat = await session.scalar(select(TaskChat).where(TaskChat.task_id == task.id))

    done = sum(1 for item in detail.checklist if item.is_completed)
    team = ", ".join(
        f"{member.user.full_name or member.user.telegram_username}"
        f"{' 👑' if member.is_leader else ''}"
        f"{' ✍️' if member.is_creator else ''}"
        for member in detail.members
    )
    lines = [
        f"📌 {task.title}",
        f"Статус: {STATUS_LABELS.get(task.status, task.status.value)}",
        f"Срок: {format_dt(task.deadline)}",
        f"Тип: {'групповая' if task.kind == TaskKind.GROUP else 'личная'}",
    ]
    if event:
        lines.append(f"Мероприятие: {event.title}")
    if task.description:
        lines.append(f"\n{task.description}")
    lines.append(f"\nЧек-лист: {done}/{len(detail.checklist)}")
    if team:
        lines.append(f"Команда: {team}")
    if report:
        lines.append(f"Отчёт: {REPORT_LABELS.get(report.status, report.status.value)}")
        if report.approval_comment:
            lines.append(f"Комментарий проверки: {report.approval_comment}")
    if task.cleanup_at:
        lines.append(f"Удаление рабочей группы: {format_dt(task.cleanup_at)}")

    rows: list[list[InlineKeyboardButton]] = []
    if task.status in OPEN_TASK_STATUSES:
        for item in detail.checklist[:20]:
            marker = "✅" if item.is_completed else "⬜"
            text = item.title if len(item.title) <= 42 else f"{item.title[:39]}…"
            rows.append(
                [InlineKeyboardButton(text=f"{marker} {text}", callback_data=f"check:{item.id}")]
            )
        rows.append(
            [InlineKeyboardButton(text="📝 Отчёт и фото", callback_data=f"report:{task.id}")]
        )
    elif report:
        rows.append([InlineKeyboardButton(text="📝 Посмотреть отчёт", callback_data=f"report:{task.id}")])

    if task.status == TaskStatus.SUBMITTED and task.leader_id == actor.id:
        rows.append(
            [
                InlineKeyboardButton(text="✅ Принять", callback_data=f"rapprove:{task.id}"),
                InlineKeyboardButton(text="↩️ На доработку", callback_data=f"rreturn:{task.id}"),
            ]
        )

    if task.kind == TaskKind.GROUP:
        if chat and chat.telegram_chat_id:
            public_id = str(chat.telegram_chat_id).replace("-100", "")
            rows.append(
                [InlineKeyboardButton(text="💬 Открыть группу", url=f"https://t.me/c/{public_id}")]
            )
        rows.append([InlineKeyboardButton(text="👥 Статус группы", callback_data=f"chat:{task.id}")])

    if can_manage_task(actor, task) and task.status in OPEN_TASK_STATUSES:
        rows.append(
            [
                InlineKeyboardButton(text="✏️ Изменить", callback_data=f"tedit:{task.id}"),
                InlineKeyboardButton(text="👥 Команда", callback_data=f"tmembers:{task.id}"),
            ]
        )
        rows.append(
            [
                InlineKeyboardButton(text="☑️ Чек-лист", callback_data=f"tcheck:{task.id}"),
                InlineKeyboardButton(text="🛑 Отменить", callback_data=f"tcancel:{task.id}"),
            ]
        )
    rows.append([InlineKeyboardButton(text="⬅️ К задачам", callback_data="tl:open")])
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=rows)


async def show_task(target: Message | CallbackQuery, actor: User, task_id: uuid.UUID) -> None:
    text, keyboard = await build_task_view(actor, task_id)
    if isinstance(target, CallbackQuery):
        try:
            await target.message.edit_text(text, reply_markup=keyboard)
        except TelegramBadRequest:
            await target.message.answer(text, reply_markup=keyboard)
        await target.answer()
    else:
        await target.answer(text, reply_markup=keyboard)


@router.message(CommandStart())
async def start(message: Message, state: FSMContext) -> None:
    user = await sync_user(message)
    if user.status in {UserStatus.INACTIVE, UserStatus.BLOCKED}:
        await message.answer("Ваш профиль отключён. Обратитесь к администратору.")
        return
    if user.status == UserStatus.NEEDS_USERNAME:
        await message.answer(
            "Укажите @username в настройках Telegram и снова отправьте /start. "
            "Username нужен для автоматического добавления в рабочие группы."
        )
        return
    if user.status != UserStatus.ACTIVE:
        await state.set_state(Registration.full_name)
        await message.answer("Добро пожаловать. Отправьте имя и фамилию.")
        return
    await state.clear()
    await message.answer("SS Bot готов к работе.", reply_markup=main_keyboard(user))


@router.message(Command("cancel"))
async def cancel_action(message: Message, state: FSMContext) -> None:
    user = await sync_user(message)
    await state.clear()
    await message.answer("Операция отменена.", reply_markup=main_keyboard(user))


@router.message(Registration.full_name, F.text)
async def receive_full_name(message: Message, state: FSMContext) -> None:
    full_name = message.text.strip()
    if len(full_name.split()) < 2 or len(full_name) > 200:
        await message.answer("Отправьте имя и фамилию, максимум 200 символов.")
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
        await session.refresh(db_user)
        status = db_user.status
    await state.clear()
    if status == UserStatus.NEEDS_USERNAME:
        await message.answer("Имя сохранено. Добавьте @username и снова отправьте /start.")
        return
    user = await sync_user(message)
    await message.answer("Регистрация завершена.", reply_markup=main_keyboard(user))


@router.message(F.text == "📋 Мои задачи")
async def my_tasks(message: Message) -> None:
    user = await sync_user(message)
    text, keyboard = await render_task_list(user, "open")
    await message.answer(text, reply_markup=keyboard)


@router.callback_query(F.data.startswith("tl:"))
async def task_list_callback(callback: CallbackQuery) -> None:
    user = await sync_callback_user(callback)
    mode = callback.data.split(":", 1)[1]
    text, keyboard = await render_task_list(user, mode)
    try:
        await callback.message.edit_text(text, reply_markup=keyboard)
    except TelegramBadRequest:
        await callback.message.answer(text, reply_markup=keyboard)
    await callback.answer()


@router.callback_query(F.data.startswith("task:"))
async def task_detail_callback(callback: CallbackQuery) -> None:
    actor = await sync_callback_user(callback)
    try:
        await show_task(callback, actor, uuid.UUID(callback.data.split(":", 1)[1]))
    except Exception as exc:
        await answer_http_error(callback, exc)


@router.message(F.text == "👤 Профиль")
async def profile(message: Message) -> None:
    user = await sync_user(message)
    async with SessionLocal() as session:
        sector = await session.get(Sector, user.sector_id) if user.sector_id else None
        assigned = await session.scalar(
            select(func.count())
            .select_from(TaskMember)
            .join(Task)
            .where(
                TaskMember.user_id == user.id,
                Task.status.in_(
                    [TaskStatus.ACTIVE, TaskStatus.RETURNED, TaskStatus.OVERDUE, TaskStatus.SUBMITTED]
                ),
            )
        )
    await message.answer(
        "\n".join(
            [
                "👤 Профиль",
                f"ФИО: {user.full_name or 'не заполнено'}",
                f"Username: @{user.telegram_username}" if user.telegram_username else "Username: нет",
                f"Роль: {ROLE_LABELS[user.role]}",
                f"Сектор: {sector.name if sector else 'не назначен'}",
                f"Текущих задач: {assigned or 0}",
            ]
        ),
        reply_markup=main_keyboard(user),
    )


@router.message(F.text == "ℹ️ Помощь")
async def help_message(message: Message) -> None:
    user = await sync_user(message)
    text = (
        "Все рабочие действия находятся в кнопках бота.\n\n"
        "📋 Мои задачи — задачи, чек-листы, отчёты и фото.\n"
        "💬 Для групповых задач бот создаёт рабочую Telegram-группу и контролирует вступление.\n"
        "📷 Фото добавляются в отчёт до его финальной отправки.\n"
        "👑 Руководитель групповой задачи принимает отчёт или возвращает его на доработку."
    )
    if user.role in {Role.ADMIN, Role.SECTOR_HEAD}:
        text += (
            "\n➕ Выдать задачу — создание личной или групповой задачи."
            "\n🗓 Мероприятия — создание мероприятий и архивы."
        )
    if user.role == Role.ADMIN:
        text += "\n⚙️ Администрирование — роли и активация пользователей."
    await message.answer(text, reply_markup=main_keyboard(user))


@router.callback_query(F.data.startswith("check:"))
async def toggle_checklist(callback: CallbackQuery) -> None:
    actor = await sync_callback_user(callback)
    item_id = uuid.UUID(callback.data.split(":", 1)[1])
    try:
        async with SessionLocal() as session:
            item = await session.get(TaskChecklistItem, item_id)
            if not item:
                raise HTTPException(status_code=404, detail="Пункт не найден")
            await task_routes.update_checklist(
                item.task_id,
                item.id,
                ChecklistUpdate(is_completed=not item.is_completed),
                actor,
                session,
            )
            task_id = item.task_id
        await show_task(callback, actor, task_id)
    except Exception as exc:
        await answer_http_error(callback, exc)


# -------------------- Task creation --------------------


@router.message(F.text == "➕ Выдать задачу")
async def issue_task_start(message: Message, state: FSMContext) -> None:
    actor = await sync_user(message)
    if actor.role not in {Role.ADMIN, Role.SECTOR_HEAD}:
        await message.answer("Недостаточно прав.")
        return
    await state.clear()
    await state.set_state(TaskIssue.title)
    await message.answer("Введите название задачи. Для отмены используйте /cancel.")


@router.message(TaskIssue.title, F.text)
async def issue_task_title(message: Message, state: FSMContext) -> None:
    title = message.text.strip()
    if not 2 <= len(title) <= 200:
        await message.answer("Название должно содержать от 2 до 200 символов.")
        return
    await state.update_data(title=title)
    await state.set_state(TaskIssue.description)
    await message.answer("Введите описание или отправьте «-».")


@router.message(TaskIssue.description, F.text)
async def issue_task_description(message: Message, state: FSMContext) -> None:
    description = message.text.strip()
    if len(description) > 5000:
        await message.answer("Описание длиннее 5000 символов.")
        return
    await state.update_data(description=None if description == "-" else description)
    actor = await sync_user(message)
    async with SessionLocal() as session:
        statement = select(Event).where(Event.purged_at.is_(None)).order_by(Event.starts_at.desc()).limit(12)
        if actor.role == Role.SECTOR_HEAD:
            statement = statement.where(Event.sector_id == actor.sector_id)
        events = list((await session.scalars(statement)).all())
    rows = [[InlineKeyboardButton(text="Без мероприятия", callback_data="issue:event:none")]]
    rows.extend(
        [
            [
                InlineKeyboardButton(
                    text=event.title[:45],
                    callback_data=f"issue:event:{event.id}",
                )
            ]
            for event in events
        ]
    )
    await state.set_state(TaskIssue.event)
    await message.answer(
        "Привязать задачу к мероприятию?",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
    )


@router.callback_query(TaskIssue.event, F.data.startswith("issue:event:"))
async def issue_task_event(callback: CallbackQuery, state: FSMContext) -> None:
    value = callback.data.rsplit(":", 1)[-1]
    await state.update_data(event_id=None if value == "none" else value)
    await state.set_state(TaskIssue.kind)
    await callback.message.answer(
        "Выберите тип задачи.",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(text="👤 Личная", callback_data="issue:kind:individual"),
                    InlineKeyboardButton(text="👥 Групповая", callback_data="issue:kind:group"),
                ]
            ]
        ),
    )
    await callback.answer()


@router.callback_query(TaskIssue.kind, F.data.startswith("issue:kind:"))
async def issue_task_kind(callback: CallbackQuery, state: FSMContext) -> None:
    kind = callback.data.rsplit(":", 1)[-1]
    await state.update_data(kind=kind, member_ids=[], leader_id=None)
    await state.set_state(TaskIssue.people)
    await callback.message.answer(
        "Введите часть ФИО или @username исполнителя. "
        "После поиска выбирайте людей кнопками и нажмите «Готово»."
    )
    await callback.answer()


@router.message(TaskIssue.people, F.text)
async def issue_task_people(message: Message, state: FSMContext) -> None:
    actor = await sync_user(message)
    data = await state.get_data()
    users = await search_users(actor, message.text, exclude_ids={actor.id})
    if not users:
        await message.answer("Никого не найдено. Введите другой запрос.")
        return
    await message.answer(
        "Выберите исполнителей:",
        reply_markup=people_keyboard(users, set(data.get("member_ids", []))),
    )


@router.callback_query(TaskIssue.people, F.data.startswith("issue:person:"))
async def issue_task_person(callback: CallbackQuery, state: FSMContext) -> None:
    user_id = callback.data.rsplit(":", 1)[-1]
    data = await state.get_data()
    selected = set(data.get("member_ids", []))
    selected.symmetric_difference_update({user_id})
    await state.update_data(member_ids=list(selected))
    visible_ids = [
        uuid.UUID(button.callback_data.rsplit(":", 1)[-1])
        for row in callback.message.reply_markup.inline_keyboard
        for button in row
        if button.callback_data and button.callback_data.startswith("issue:person:")
    ]
    async with SessionLocal() as session:
        users = list((await session.scalars(select(User).where(User.id.in_(visible_ids)))).all())
    by_id = {user.id: user for user in users}
    ordered = [by_id[item] for item in visible_ids if item in by_id]
    await callback.message.edit_reply_markup(reply_markup=people_keyboard(ordered, selected))
    await callback.answer("Выбор обновлён")


async def leader_keyboard(member_ids: list[str]) -> InlineKeyboardMarkup:
    ids = [uuid.UUID(value) for value in member_ids]
    async with SessionLocal() as session:
        users = list((await session.scalars(select(User).where(User.id.in_(ids)))).all())
    by_id = {user.id: user for user in users}
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=f"👑 {display_user(by_id[item])}",
                    callback_data=f"issue:leader:{item}",
                )
            ]
            for item in ids
            if item in by_id
        ]
    )


@router.callback_query(TaskIssue.people, F.data == "issue:people:done")
async def issue_task_people_done(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    count = len(data.get("member_ids", []))
    if count == 0 or (data["kind"] == "individual" and count != 1):
        await callback.answer(
            "Для личной задачи нужен один исполнитель, для групповой — минимум один.",
            show_alert=True,
        )
        return
    if data["kind"] == "group":
        await state.set_state(TaskIssue.leader)
        await callback.message.answer(
            "Выберите руководителя задачи.",
            reply_markup=await leader_keyboard(data["member_ids"]),
        )
    else:
        await state.set_state(TaskIssue.deadline)
        await callback.message.answer("Введите дедлайн: ДД.ММ.ГГГГ ЧЧ:ММ.")
    await callback.answer()


@router.callback_query(TaskIssue.leader, F.data.startswith("issue:leader:"))
async def issue_task_leader(callback: CallbackQuery, state: FSMContext) -> None:
    leader_id = callback.data.rsplit(":", 1)[-1]
    data = await state.get_data()
    if leader_id not in set(data.get("member_ids", [])):
        await callback.answer("Исполнитель больше не выбран.", show_alert=True)
        return
    await state.update_data(leader_id=leader_id)
    await state.set_state(TaskIssue.deadline)
    await callback.message.answer("Введите дедлайн: ДД.ММ.ГГГГ ЧЧ:ММ.")
    await callback.answer()


@router.message(TaskIssue.deadline, F.text)
async def issue_task_deadline(message: Message, state: FSMContext) -> None:
    try:
        deadline = parse_input_datetime(message.text)
    except ValueError:
        await message.answer("Формат: ДД.ММ.ГГГГ ЧЧ:ММ.")
        return
    if deadline <= datetime.now(UTC):
        await message.answer("Дедлайн должен быть в будущем.")
        return
    await state.update_data(deadline=deadline.isoformat())
    await state.set_state(TaskIssue.checklist)
    await message.answer(
        "Введите чек-лист: каждый пункт с новой строки. Если не нужен — отправьте «-»."
    )


@router.message(TaskIssue.checklist, F.text)
async def issue_task_checklist(message: Message, state: FSMContext) -> None:
    raw = message.text.strip()
    checklist = [] if raw == "-" else [line.strip() for line in raw.splitlines() if line.strip()]
    if len(checklist) > 50 or any(len(item) > 500 for item in checklist):
        await message.answer("Максимум 50 пунктов, до 500 символов каждый.")
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
                    description=data.get("description"),
                    kind=TaskKind(data["kind"]),
                    deadline=datetime.fromisoformat(data["deadline"]),
                    event_id=uuid.UUID(data["event_id"]) if data.get("event_id") else None,
                    leader_id=uuid.UUID(data["leader_id"]) if data.get("leader_id") else None,
                    member_ids=[uuid.UUID(value) for value in data["member_ids"]],
                    checklist=checklist,
                ),
                str(uuid.uuid4()),
            )
            await session.commit()
            task_id = task.id
        await state.clear()
        await message.answer(
            "Задача создана. Уведомления и рабочая группа будут обработаны автоматически.",
            reply_markup=main_keyboard(actor),
        )
        await show_task(message, actor, task_id)
    except Exception as exc:
        await answer_http_error(message, exc)


# -------------------- Task management --------------------


@router.callback_query(F.data.startswith("tedit:"))
async def task_edit_menu(callback: CallbackQuery) -> None:
    actor = await sync_callback_user(callback)
    task_id = uuid.UUID(callback.data.split(":", 1)[1])
    async with SessionLocal() as session:
        task = await session.get(Task, task_id)
    if not task or not can_manage_task(actor, task) or task.status not in OPEN_TASK_STATUSES:
        await callback.answer("Изменение недоступно.", show_alert=True)
        return
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Название", callback_data=f"tef:title:{task.id}")],
            [InlineKeyboardButton(text="Описание", callback_data=f"tef:description:{task.id}")],
            [InlineKeyboardButton(text="Дедлайн", callback_data=f"tef:deadline:{task.id}")],
            [InlineKeyboardButton(text="⬅️ Назад", callback_data=f"task:{task.id}")],
        ]
    )
    await callback.message.edit_text("✏️ Что изменить?", reply_markup=keyboard)
    await callback.answer()


@router.callback_query(F.data.startswith("tef:"))
async def task_edit_field(callback: CallbackQuery, state: FSMContext) -> None:
    _, field, task_id = callback.data.split(":", 2)
    if field not in {"title", "description", "deadline"}:
        await callback.answer("Неизвестное поле.", show_alert=True)
        return
    await state.set_state(TaskEdit.value)
    await state.update_data(task_id=task_id, field=field)
    prompts = {
        "title": "Введите новое название.",
        "description": "Введите новое описание или «-», чтобы очистить.",
        "deadline": "Введите новый дедлайн: ДД.ММ.ГГГГ ЧЧ:ММ.",
    }
    await callback.message.answer(prompts[field])
    await callback.answer()


@router.message(TaskEdit.value, F.text)
async def task_edit_value(message: Message, state: FSMContext) -> None:
    actor = await sync_user(message)
    data = await state.get_data()
    task_id = uuid.UUID(data["task_id"])
    field = data["field"]
    try:
        if field == "title":
            value = message.text.strip()
            if not 2 <= len(value) <= 200:
                raise HTTPException(status_code=422, detail="Название: от 2 до 200 символов")
            payload = TaskUpdate(title=value)
        elif field == "description":
            text = message.text.strip()
            payload = TaskUpdate(description=None if text == "-" else text)
        else:
            deadline = parse_input_datetime(message.text)
            payload = TaskUpdate(deadline=deadline)
        async with SessionLocal() as session:
            await task_routes.update_task(task_id, payload, actor, session)
        await state.clear()
        await show_task(message, actor, task_id)
    except ValueError:
        await message.answer("Формат даты: ДД.ММ.ГГГГ ЧЧ:ММ.")
    except Exception as exc:
        await answer_http_error(message, exc)


@router.callback_query(F.data.startswith("tmembers:"))
async def task_members_menu(callback: CallbackQuery) -> None:
    actor = await sync_callback_user(callback)
    task_id = uuid.UUID(callback.data.split(":", 1)[1])
    try:
        async with SessionLocal() as session:
            task = await session.get(Task, task_id)
            if not task or not can_manage_task(actor, task):
                raise HTTPException(status_code=403, detail="Нет доступа")
            rows = list(
                (
                    await session.execute(
                        select(TaskMember, User)
                        .join(User, TaskMember.user_id == User.id)
                        .where(TaskMember.task_id == task.id)
                    )
                ).all()
            )
        buttons: list[list[InlineKeyboardButton]] = []
        lines = ["👥 Команда задачи"]
        for member, user in rows:
            role = "автор" if member.is_creator else "руководитель" if member.is_leader else "участник"
            lines.append(f"• {display_user(user)} — {role}")
            if (
                task.kind == TaskKind.GROUP
                and task.status in OPEN_TASK_STATUSES
                and not member.is_creator
                and not member.is_leader
            ):
                buttons.append(
                    [
                        InlineKeyboardButton(
                            text=f"🗑 Убрать {display_user(user)[:28]}",
                            callback_data=f"tmrem:{member.id}",
                        )
                    ]
                )
        if task.kind == TaskKind.GROUP and task.status in OPEN_TASK_STATUSES:
            buttons.append([InlineKeyboardButton(text="➕ Добавить", callback_data=f"tmadd:{task.id}")])
            buttons.append(
                [InlineKeyboardButton(text="👑 Сменить руководителя", callback_data=f"tmleader:{task.id}")]
            )
        buttons.append([InlineKeyboardButton(text="⬅️ Назад", callback_data=f"task:{task.id}")])
        await callback.message.edit_text(
            "\n".join(lines),
            reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
        )
        await callback.answer()
    except Exception as exc:
        await answer_http_error(callback, exc)


@router.callback_query(F.data.startswith("tmadd:"))
async def task_member_add_start(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(TaskMemberAdd.query)
    await state.update_data(task_id=callback.data.split(":", 1)[1])
    await callback.message.answer("Введите ФИО или @username нового участника.")
    await callback.answer()


@router.message(TaskMemberAdd.query, F.text)
async def task_member_add_search(message: Message, state: FSMContext) -> None:
    actor = await sync_user(message)
    data = await state.get_data()
    task_id = uuid.UUID(data["task_id"])
    async with SessionLocal() as session:
        member_ids = set(
            (
                await session.scalars(
                    select(TaskMember.user_id).where(TaskMember.task_id == task_id)
                )
            ).all()
        )
    users = await search_users(actor, message.text, exclude_ids=member_ids)
    if not users:
        await message.answer("Никого не найдено.")
        return
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=f"➕ {display_user(user)}",
                    callback_data=f"tma:{user.id}",
                )
            ]
            for user in users
        ]
    )
    await message.answer("Кого добавить?", reply_markup=keyboard)


@router.callback_query(TaskMemberAdd.query, F.data.startswith("tma:"))
async def task_member_add_finish(callback: CallbackQuery, state: FSMContext) -> None:
    actor = await sync_callback_user(callback)
    data = await state.get_data()
    task_id = uuid.UUID(data["task_id"])
    user_id = uuid.UUID(callback.data.split(":", 1)[1])
    try:
        async with SessionLocal() as session:
            await task_routes.add_task_member(
                task_id,
                TaskMemberCreate(user_id=user_id),
                actor,
                session,
            )
        await state.clear()
        await callback.answer("Участник добавлен")
        await show_task(callback, actor, task_id)
    except Exception as exc:
        await answer_http_error(callback, exc)


@router.callback_query(F.data.startswith("tmrem:"))
async def task_member_remove(callback: CallbackQuery) -> None:
    actor = await sync_callback_user(callback)
    member_id = uuid.UUID(callback.data.split(":", 1)[1])
    try:
        async with SessionLocal() as session:
            member = await session.get(TaskMember, member_id)
            if not member:
                raise HTTPException(status_code=404, detail="Участник не найден")
            task_id, user_id = member.task_id, member.user_id
            await task_routes.remove_task_member(task_id, user_id, actor, session)
        await callback.answer("Участник удалён")
        await show_task(callback, actor, task_id)
    except Exception as exc:
        await answer_http_error(callback, exc)


@router.callback_query(F.data.startswith("tmleader:"))
async def task_leader_menu(callback: CallbackQuery) -> None:
    actor = await sync_callback_user(callback)
    task_id = uuid.UUID(callback.data.split(":", 1)[1])
    try:
        async with SessionLocal() as session:
            task = await session.get(Task, task_id)
            if not task or not can_manage_task(actor, task) or task.kind != TaskKind.GROUP:
                raise HTTPException(status_code=403, detail="Недоступно")
            rows = list(
                (
                    await session.execute(
                        select(TaskMember, User)
                        .join(User, TaskMember.user_id == User.id)
                        .where(TaskMember.task_id == task.id, TaskMember.is_creator.is_(False))
                    )
                ).all()
            )
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text=f"👑 {display_user(user)}",
                        callback_data=f"tml:{member.id}",
                    )
                ]
                for member, user in rows
            ]
            + [[InlineKeyboardButton(text="⬅️ Назад", callback_data=f"tmembers:{task.id}")]]
        )
        await callback.message.edit_text("Выберите нового руководителя.", reply_markup=keyboard)
        await callback.answer()
    except Exception as exc:
        await answer_http_error(callback, exc)


@router.callback_query(F.data.startswith("tml:"))
async def task_leader_change(callback: CallbackQuery) -> None:
    actor = await sync_callback_user(callback)
    member_id = uuid.UUID(callback.data.split(":", 1)[1])
    try:
        async with SessionLocal() as session:
            member = await session.get(TaskMember, member_id)
            if not member:
                raise HTTPException(status_code=404, detail="Участник не найден")
            await task_routes.update_task(
                member.task_id,
                TaskUpdate(leader_id=member.user_id),
                actor,
                session,
            )
            task_id = member.task_id
        await callback.answer("Руководитель изменён")
        await show_task(callback, actor, task_id)
    except Exception as exc:
        await answer_http_error(callback, exc)


@router.callback_query(F.data.startswith("tcheck:"))
async def task_checklist_manage(callback: CallbackQuery) -> None:
    actor = await sync_callback_user(callback)
    task_id = uuid.UUID(callback.data.split(":", 1)[1])
    try:
        async with SessionLocal() as session:
            task = await session.get(Task, task_id)
            if not task or not can_manage_task(actor, task):
                raise HTTPException(status_code=403, detail="Нет доступа")
            items = list(
                (
                    await session.scalars(
                        select(TaskChecklistItem)
                        .where(TaskChecklistItem.task_id == task.id)
                        .order_by(TaskChecklistItem.position)
                    )
                ).all()
            )
        rows = [
            [
                InlineKeyboardButton(
                    text=f"🗑 {item.title[:42]}",
                    callback_data=f"cldel:{item.id}",
                )
            ]
            for item in items
        ]
        rows.append([InlineKeyboardButton(text="➕ Добавить пункт", callback_data=f"cladd:{task.id}")])
        rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data=f"task:{task.id}")])
        await callback.message.edit_text(
            "☑️ Управление чек-листом",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
        )
        await callback.answer()
    except Exception as exc:
        await answer_http_error(callback, exc)


@router.callback_query(F.data.startswith("cladd:"))
async def checklist_add_start(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(ChecklistAdd.title)
    await state.update_data(task_id=callback.data.split(":", 1)[1])
    await callback.message.answer("Введите новый пункт чек-листа.")
    await callback.answer()


@router.message(ChecklistAdd.title, F.text)
async def checklist_add_finish(message: Message, state: FSMContext) -> None:
    actor = await sync_user(message)
    data = await state.get_data()
    title = message.text.strip()
    if not 1 <= len(title) <= 500:
        await message.answer("Пункт должен содержать от 1 до 500 символов.")
        return
    task_id = uuid.UUID(data["task_id"])
    try:
        async with SessionLocal() as session:
            await task_routes.add_checklist_item(
                task_id,
                ChecklistItemCreate(title=title),
                actor,
                session,
            )
        await state.clear()
        await show_task(message, actor, task_id)
    except Exception as exc:
        await answer_http_error(message, exc)


@router.callback_query(F.data.startswith("cldel:"))
async def checklist_delete(callback: CallbackQuery) -> None:
    actor = await sync_callback_user(callback)
    item_id = uuid.UUID(callback.data.split(":", 1)[1])
    try:
        async with SessionLocal() as session:
            item = await session.get(TaskChecklistItem, item_id)
            if not item:
                raise HTTPException(status_code=404, detail="Пункт не найден")
            task_id = item.task_id
            await task_routes.delete_checklist_item(task_id, item.id, actor, session)
        await callback.answer("Пункт удалён")
        await show_task(callback, actor, task_id)
    except Exception as exc:
        await answer_http_error(callback, exc)


@router.callback_query(F.data.startswith("tcancel:"))
async def task_cancel_confirm(callback: CallbackQuery) -> None:
    task_id = callback.data.split(":", 1)[1]
    await callback.message.edit_text(
        "Точно отменить задачу?",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(text="Да, отменить", callback_data=f"tcok:{task_id}"),
                    InlineKeyboardButton(text="Нет", callback_data=f"task:{task_id}"),
                ]
            ]
        ),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("tcok:"))
async def task_cancel_finish(callback: CallbackQuery) -> None:
    actor = await sync_callback_user(callback)
    task_id = uuid.UUID(callback.data.split(":", 1)[1])
    try:
        async with SessionLocal() as session:
            await task_routes.cancel_task(task_id, actor, session)
        await callback.answer("Задача отменена")
        await show_task(callback, actor, task_id)
    except Exception as exc:
        await answer_http_error(callback, exc)


# -------------------- Reports and photos --------------------


async def build_report_view(actor: User, task_id: uuid.UUID) -> tuple[str, InlineKeyboardMarkup]:
    async with SessionLocal() as session:
        task = await session.get(Task, task_id)
        if not task:
            raise HTTPException(status_code=404, detail="Задача не найдена")
        if not await session.scalar(
            select(TaskMember.id).where(
                TaskMember.task_id == task.id,
                TaskMember.user_id == actor.id,
            )
        ):
            raise HTTPException(status_code=403, detail="Нет доступа")
        report = await session.scalar(select(TaskReport).where(TaskReport.task_id == task.id))
        photos: list[TaskPhoto] = []
        if report:
            photos = list(
                (
                    await session.scalars(
                        select(TaskPhoto)
                        .where(TaskPhoto.report_id == report.id)
                        .order_by(TaskPhoto.created_at)
                    )
                ).all()
            )
    lines = [f"📝 Отчёт: {task.title}"]
    if report:
        lines.append(f"Статус: {REPORT_LABELS.get(report.status, report.status.value)}")
        lines.append(f"Комментарий: {report.comment or 'не указан'}")
        lines.append(f"Фото: {len(photos)}/5")
        if report.approval_comment:
            lines.append(f"Результат проверки: {report.approval_comment}")
    else:
        lines.extend(["Отчёт ещё не создан.", "Фото: 0/5"])
    rows: list[list[InlineKeyboardButton]] = []
    if task.status in OPEN_TASK_STATUSES:
        rows.append(
            [
                InlineKeyboardButton(text="✍️ Комментарий", callback_data=f"rcomment:{task.id}"),
                InlineKeyboardButton(text="📷 Добавить фото", callback_data=f"rphoto:{task.id}"),
            ]
        )
        for index, photo in enumerate(photos, start=1):
            rows.append(
                [
                    InlineKeyboardButton(text=f"👀 Фото {index}", callback_data=f"rshow:{photo.id}"),
                    InlineKeyboardButton(text=f"🗑 Фото {index}", callback_data=f"rdel:{photo.id}"),
                ]
            )
        rows.append([InlineKeyboardButton(text="✅ Отправить отчёт", callback_data=f"rsubmit:{task.id}")])
    else:
        for index, photo in enumerate(photos, start=1):
            rows.append(
                [InlineKeyboardButton(text=f"👀 Фото {index}", callback_data=f"rshow:{photo.id}")]
            )
    if task.status == TaskStatus.SUBMITTED and task.leader_id == actor.id:
        rows.append(
            [
                InlineKeyboardButton(text="✅ Принять", callback_data=f"rapprove:{task.id}"),
                InlineKeyboardButton(text="↩️ На доработку", callback_data=f"rreturn:{task.id}"),
            ]
        )
    rows.append([InlineKeyboardButton(text="⬅️ К задаче", callback_data=f"task:{task.id}")])
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=rows)


async def show_report(target: Message | CallbackQuery, actor: User, task_id: uuid.UUID) -> None:
    text, keyboard = await build_report_view(actor, task_id)
    if isinstance(target, CallbackQuery):
        try:
            await target.message.edit_text(text, reply_markup=keyboard)
        except TelegramBadRequest:
            await target.message.answer(text, reply_markup=keyboard)
        await target.answer()
    else:
        await target.answer(text, reply_markup=keyboard)


@router.callback_query(F.data.startswith("report:"))
async def report_menu(callback: CallbackQuery) -> None:
    actor = await sync_callback_user(callback)
    try:
        await show_report(callback, actor, uuid.UUID(callback.data.split(":", 1)[1]))
    except Exception as exc:
        await answer_http_error(callback, exc)


@router.callback_query(F.data.startswith("rcomment:"))
async def report_comment_start(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(ReportFlow.comment)
    await state.update_data(task_id=callback.data.split(":", 1)[1])
    await callback.message.answer("Введите комментарий к отчёту. Для очистки отправьте «-».")
    await callback.answer()


@router.message(ReportFlow.comment, F.text)
async def report_comment_save(message: Message, state: FSMContext) -> None:
    actor = await sync_user(message)
    data = await state.get_data()
    task_id = uuid.UUID(data["task_id"])
    comment = message.text.strip()
    if len(comment) > 5000:
        await message.answer("Максимум 5000 символов.")
        return
    try:
        async with SessionLocal() as session:
            task = await report_routes.require_task_member(session, task_id, actor)
            if task.status not in OPEN_TASK_STATUSES:
                raise HTTPException(status_code=422, detail="Отчёт уже нельзя редактировать")
            report = await report_routes.get_or_create_editable_report(session, task, actor, lock=True)
            report.comment = None if comment == "-" else comment
            report.submitted_by_id = actor.id
            await session.commit()
        await state.clear()
        await show_report(message, actor, task_id)
    except Exception as exc:
        await answer_http_error(message, exc)


@router.callback_query(F.data.startswith("rphoto:"))
async def report_photo_start(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(ReportFlow.photo)
    await state.update_data(task_id=callback.data.split(":", 1)[1])
    await callback.message.answer(
        "Отправьте фотографию. Можно обычным фото или файлом JPEG/PNG/WebP, до 10 МБ."
    )
    await callback.answer()


async def download_image_bytes(message: Message) -> tuple[bytes, str]:
    if message.photo:
        media = message.photo[-1]
        content_type = "image/jpeg"
        size = media.file_size or 0
        file_id = media.file_id
    elif message.document and message.document.mime_type in {
        "image/jpeg",
        "image/png",
        "image/webp",
    }:
        media = message.document
        content_type = message.document.mime_type
        size = message.document.file_size or 0
        file_id = message.document.file_id
    else:
        raise ValueError("Отправьте JPEG, PNG или WebP.")
    if size and size > 10 * 1024 * 1024:
        raise ValueError("Фото должно быть не больше 10 МБ.")
    file = await message.bot.get_file(file_id)
    buffer = BytesIO()
    await message.bot.download_file(file.file_path, destination=buffer)
    payload = buffer.getvalue()
    if not payload or len(payload) > 10 * 1024 * 1024:
        raise ValueError("Фото должно быть от 1 байта до 10 МБ.")
    return payload, content_type


@router.message(ReportFlow.photo, F.photo | F.document)
async def report_photo_save(message: Message, state: FSMContext) -> None:
    actor = await sync_user(message)
    data = await state.get_data()
    task_id = uuid.UUID(data["task_id"])
    try:
        payload, _ = await download_image_bytes(message)
        processed = inspect_photo(payload)
        async with SessionLocal() as session:
            task = await report_routes.require_task_member(session, task_id, actor)
            if task.status not in OPEN_TASK_STATUSES:
                raise HTTPException(status_code=422, detail="Фото уже нельзя добавлять")
            report = await report_routes.get_or_create_editable_report(session, task, actor, lock=True)
            count = await session.scalar(
                select(func.count()).select_from(TaskPhoto).where(TaskPhoto.report_id == report.id)
            )
            if count >= 5:
                raise HTTPException(status_code=422, detail="В отчёте уже 5 фотографий")
            extension = {
                "image/jpeg": "jpg",
                "image/png": "png",
                "image/webp": "webp",
            }[processed.content_type]
            original_key = f"tasks/{task.id}/{uuid.uuid4()}.{extension}"
            preview_key = f"previews/{task.id}/{uuid.uuid4()}.jpg"
            put_object(original_key, payload, processed.content_type)
            try:
                put_object(preview_key, processed.preview_bytes, "image/jpeg")
            except Exception:
                delete_object(original_key)
                raise
            photo = TaskPhoto(
                report_id=report.id,
                object_key=original_key,
                content_type=processed.content_type,
                size_bytes=len(payload),
                preview_object_key=preview_key,
                width=processed.width,
                height=processed.height,
                uploaded_by_id=actor.id,
            )
            session.add(photo)
            await session.commit()
        await state.clear()
        await message.answer("Фото сохранено.")
        await show_report(message, actor, task_id)
    except ValueError as exc:
        await message.answer(str(exc))
    except Exception as exc:
        await answer_http_error(message, exc)


@router.message(ReportFlow.photo)
async def report_photo_wrong_type(message: Message) -> None:
    await message.answer("Нужно отправить изображение JPEG/PNG/WebP.")


@router.callback_query(F.data.startswith("rshow:"))
async def report_photo_show(callback: CallbackQuery) -> None:
    actor = await sync_callback_user(callback)
    photo_id = uuid.UUID(callback.data.split(":", 1)[1])
    try:
        async with SessionLocal() as session:
            photo = await session.get(TaskPhoto, photo_id)
            if not photo:
                raise HTTPException(status_code=404, detail="Фото не найдено")
            report = await session.get(TaskReport, photo.report_id)
            if not report or not await session.scalar(
                select(TaskMember.id).where(
                    TaskMember.task_id == report.task_id,
                    TaskMember.user_id == actor.id,
                )
            ):
                raise HTTPException(status_code=403, detail="Нет доступа")
            object_key = photo.preview_object_key or photo.object_key
        payload = get_object_bytes(object_key)
        await callback.message.answer_photo(
            BufferedInputFile(payload, filename=f"report-{photo.id}.jpg")
        )
        await callback.answer()
    except Exception as exc:
        await answer_http_error(callback, exc)


@router.callback_query(F.data.startswith("rdel:"))
async def report_photo_delete(callback: CallbackQuery) -> None:
    actor = await sync_callback_user(callback)
    photo_id = uuid.UUID(callback.data.split(":", 1)[1])
    try:
        async with SessionLocal() as session:
            photo = await session.get(TaskPhoto, photo_id)
            if not photo:
                raise HTTPException(status_code=404, detail="Фото не найдено")
            report = await session.get(TaskReport, photo.report_id)
            if not report:
                raise HTTPException(status_code=404, detail="Отчёт не найден")
            task_id = report.task_id
            await report_routes.delete_report_photo(task_id, photo.id, actor, session)
        await callback.answer("Фото удалено")
        await show_report(callback, actor, task_id)
    except Exception as exc:
        await answer_http_error(callback, exc)


@router.callback_query(F.data.startswith("rsubmit:"))
async def report_submit(callback: CallbackQuery) -> None:
    actor = await sync_callback_user(callback)
    task_id = uuid.UUID(callback.data.split(":", 1)[1])
    try:
        async with SessionLocal() as session:
            report = await session.scalar(select(TaskReport).where(TaskReport.task_id == task_id))
            comment = report.comment if report else None
            await report_routes.submit_report(
                task_id,
                ReportCreate(comment=comment),
                actor,
                session,
            )
        await callback.answer("Отчёт отправлен")
        await show_task(callback, actor, task_id)
    except Exception as exc:
        await answer_http_error(callback, exc)


@router.callback_query(F.data.startswith("rapprove:"))
async def report_approve(callback: CallbackQuery) -> None:
    actor = await sync_callback_user(callback)
    task_id = uuid.UUID(callback.data.split(":", 1)[1])
    try:
        async with SessionLocal() as session:
            await report_routes.decide_report(
                task_id,
                ReportDecision(approved=True),
                actor,
                session,
            )
        await callback.answer("Отчёт принят")
        await show_task(callback, actor, task_id)
    except Exception as exc:
        await answer_http_error(callback, exc)


@router.callback_query(F.data.startswith("rreturn:"))
async def report_return_start(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(ReportFlow.return_reason)
    await state.update_data(task_id=callback.data.split(":", 1)[1])
    await callback.message.answer("Введите причину возврата отчёта на доработку.")
    await callback.answer()


@router.message(ReportFlow.return_reason, F.text)
async def report_return_finish(message: Message, state: FSMContext) -> None:
    actor = await sync_user(message)
    data = await state.get_data()
    reason = message.text.strip()
    if not reason:
        await message.answer("Причина обязательна.")
        return
    task_id = uuid.UUID(data["task_id"])
    try:
        async with SessionLocal() as session:
            await report_routes.decide_report(
                task_id,
                ReportDecision(approved=False, reason=reason),
                actor,
                session,
            )
        await state.clear()
        await message.answer("Отчёт возвращён на доработку.")
        await show_task(message, actor, task_id)
    except Exception as exc:
        await answer_http_error(message, exc)


# -------------------- Telegram group state --------------------


@router.callback_query(F.data.startswith("chat:"))
async def chat_status(callback: CallbackQuery) -> None:
    actor = await sync_callback_user(callback)
    task_id = uuid.UUID(callback.data.split(":", 1)[1])
    try:
        async with SessionLocal() as session:
            task = await session.get(Task, task_id)
            if not task:
                raise HTTPException(status_code=404, detail="Задача не найдена")
            chat = await session.scalar(select(TaskChat).where(TaskChat.task_id == task.id))
            if not chat:
                raise HTTPException(status_code=404, detail="Рабочей группы нет")
            members = list(
                (
                    await session.execute(
                        select(TaskChatMember, User)
                        .join(User, TaskChatMember.user_id == User.id)
                        .where(TaskChatMember.task_chat_id == chat.id)
                    )
                ).all()
            )
        lines = [
            "💬 Рабочая группа",
            f"Статус: {chat.status.value}",
        ]
        if chat.last_error:
            lines.append(f"Ошибка: {chat.last_error}")
        rows: list[list[InlineKeyboardButton]] = []
        for member, user in members:
            lines.append(
                f"• {display_user(user)} — {member.state.value}, напоминаний: {member.reminder_count}"
            )
            if (
                can_manage_task(actor, task)
                and chat.status == ChatStatus.READY
                and member.state not in {MembershipState.JOINED, MembershipState.REMOVED}
            ):
                rows.append(
                    [
                        InlineKeyboardButton(
                            text=f"🔁 Пригласить {display_user(user)[:24]}",
                            callback_data=f"chmretry:{member.id}",
                        )
                    ]
                )
        if can_manage_task(actor, task) and chat.status in {ChatStatus.FAILED, ChatStatus.DEGRADED}:
            action = "recover" if chat.telegram_chat_id else "retry"
            rows.append(
                [
                    InlineKeyboardButton(
                        text="🛠 Восстановить группу",
                        callback_data=f"ch{action}:{task.id}",
                    )
                ]
            )
        if chat.telegram_chat_id:
            public_id = str(chat.telegram_chat_id).replace("-100", "")
            rows.append(
                [InlineKeyboardButton(text="💬 Открыть", url=f"https://t.me/c/{public_id}")]
            )
        rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data=f"task:{task.id}")])
        await callback.message.edit_text(
            "\n".join(lines),
            reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
        )
        await callback.answer()
    except Exception as exc:
        await answer_http_error(callback, exc)


@router.callback_query(F.data.startswith("chretry:"))
async def chat_retry(callback: CallbackQuery) -> None:
    actor = await sync_callback_user(callback)
    task_id = uuid.UUID(callback.data.split(":", 1)[1])
    try:
        async with SessionLocal() as session:
            await task_routes.retry_task_chat(task_id, actor, session)
        await callback.answer("Повторное создание поставлено в очередь")
        await callback.message.edit_text(
            "Создание рабочей группы повторно поставлено в очередь.",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [InlineKeyboardButton(text="🔄 Обновить статус", callback_data=f"chat:{task_id}")]
                ]
            ),
        )
    except Exception as exc:
        await answer_http_error(callback, exc)


@router.callback_query(F.data.startswith("chrecover:"))
async def chat_recover(callback: CallbackQuery) -> None:
    actor = await sync_callback_user(callback)
    task_id = uuid.UUID(callback.data.split(":", 1)[1])
    try:
        async with SessionLocal() as session:
            await task_routes.recover_task_chat(task_id, actor, session)
        await callback.answer("Восстановление поставлено в очередь")
        await callback.message.edit_text(
            "Восстановление рабочей группы поставлено в очередь.",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [InlineKeyboardButton(text="🔄 Обновить статус", callback_data=f"chat:{task_id}")]
                ]
            ),
        )
    except Exception as exc:
        await answer_http_error(callback, exc)


@router.callback_query(F.data.startswith("chmretry:"))
async def chat_member_retry(callback: CallbackQuery) -> None:
    actor = await sync_callback_user(callback)
    member_id = uuid.UUID(callback.data.split(":", 1)[1])
    try:
        async with SessionLocal() as session:
            chat_member = await session.get(TaskChatMember, member_id)
            if not chat_member:
                raise HTTPException(status_code=404, detail="Участник группы не найден")
            chat = await session.get(TaskChat, chat_member.task_chat_id)
            if not chat:
                raise HTTPException(status_code=404, detail="Группа не найдена")
            await task_routes.retry_task_chat_member(
                chat.task_id,
                chat_member.user_id,
                actor,
                session,
            )
            task_id = chat.task_id
        await callback.answer("Приглашение поставлено в очередь")
        await callback.message.edit_text(
            "Повторное приглашение участника поставлено в очередь.",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [InlineKeyboardButton(text="🔄 Обновить статус", callback_data=f"chat:{task_id}")]
                ]
            ),
        )
    except Exception as exc:
        await answer_http_error(callback, exc)


# -------------------- Events --------------------


@router.message(F.text == "🗓 Мероприятия")
async def events_menu(message: Message) -> None:
    actor = await sync_user(message)
    if actor.role not in {Role.ADMIN, Role.SECTOR_HEAD}:
        await message.answer("Недостаточно прав.")
        return
    async with SessionLocal() as session:
        statement = select(Event).where(Event.purged_at.is_(None)).order_by(Event.starts_at.desc()).limit(20)
        if actor.role == Role.SECTOR_HEAD:
            statement = statement.where(Event.sector_id == actor.sector_id)
        events = list((await session.scalars(statement)).all())
    rows = [
        [InlineKeyboardButton(text="➕ Новое мероприятие", callback_data="event:new")]
    ]
    rows.extend(
        [
            [
                InlineKeyboardButton(
                    text=f"{event.title[:34]} · {format_dt(event.starts_at)[:10]}",
                    callback_data=f"event:{event.id}",
                )
            ]
            for event in events
        ]
    )
    await message.answer(
        "🗓 Мероприятия",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
    )


@router.callback_query(F.data == "event:new")
async def event_create_start(callback: CallbackQuery, state: FSMContext) -> None:
    actor = await sync_callback_user(callback)
    if actor.role not in {Role.ADMIN, Role.SECTOR_HEAD}:
        await callback.answer("Недостаточно прав.", show_alert=True)
        return
    await state.clear()
    await state.set_state(EventCreateFlow.title)
    await callback.message.answer("Введите название мероприятия.")
    await callback.answer()


@router.message(EventCreateFlow.title, F.text)
async def event_create_title(message: Message, state: FSMContext) -> None:
    title = message.text.strip()
    if not 2 <= len(title) <= 200:
        await message.answer("Название: от 2 до 200 символов.")
        return
    await state.update_data(title=title)
    await state.set_state(EventCreateFlow.starts_at)
    await message.answer("Дата начала: ДД.ММ.ГГГГ ЧЧ:ММ.")


@router.message(EventCreateFlow.starts_at, F.text)
async def event_create_start_date(message: Message, state: FSMContext) -> None:
    try:
        value = parse_input_datetime(message.text)
    except ValueError:
        await message.answer("Формат: ДД.ММ.ГГГГ ЧЧ:ММ.")
        return
    await state.update_data(starts_at=value.isoformat())
    await state.set_state(EventCreateFlow.ends_at)
    await message.answer("Дата окончания в том же формате или «-», если не нужна.")


@router.message(EventCreateFlow.ends_at, F.text)
async def event_create_end_date(message: Message, state: FSMContext) -> None:
    text = message.text.strip()
    if text == "-":
        value = None
    else:
        try:
            value = parse_input_datetime(text)
        except ValueError:
            await message.answer("Формат: ДД.ММ.ГГГГ ЧЧ:ММ или «-».")
            return
    await state.update_data(ends_at=value.isoformat() if value else None)
    await state.set_state(EventCreateFlow.budget)
    await message.answer("Бюджет числом или «-», если не нужен.")


@router.message(EventCreateFlow.budget, F.text)
async def event_create_budget(message: Message, state: FSMContext) -> None:
    text = message.text.strip().replace(",", ".")
    if text == "-":
        budget = None
    else:
        try:
            budget = float(text)
        except ValueError:
            await message.answer("Введите число или «-».")
            return
        if budget < 0:
            await message.answer("Бюджет не может быть отрицательным.")
            return
    await state.update_data(budget=budget)
    await state.set_state(EventCreateFlow.description)
    await message.answer("Описание мероприятия или «-».")


@router.message(EventCreateFlow.description, F.text)
async def event_create_description(message: Message, state: FSMContext) -> None:
    description = message.text.strip()
    if len(description) > 5000:
        await message.answer("Максимум 5000 символов.")
        return
    await state.update_data(description=None if description == "-" else description, participant_ids=[])
    await state.set_state(EventCreateFlow.people)
    await message.answer(
        "Добавьте участников: введите ФИО или @username. "
        "Если участников заранее указывать не нужно, отправьте «-»."
    )


@router.message(EventCreateFlow.people, F.text)
async def event_create_people_search(message: Message, state: FSMContext) -> None:
    actor = await sync_user(message)
    data = await state.get_data()
    if message.text.strip() == "-":
        await create_event_from_state(message, state, actor)
        return
    users = await search_users(actor, message.text)
    if not users:
        await message.answer("Никого не найдено.")
        return
    await message.answer(
        "Выберите участников:",
        reply_markup=event_people_keyboard(users, set(data.get("participant_ids", []))),
    )


@router.callback_query(EventCreateFlow.people, F.data.startswith("evp:"))
async def event_create_people_callback(callback: CallbackQuery, state: FSMContext) -> None:
    value = callback.data.split(":", 1)[1]
    if value == "done":
        actor = await sync_callback_user(callback)
        await callback.answer()
        await create_event_from_state(callback.message, state, actor)
        return
    data = await state.get_data()
    selected = set(data.get("participant_ids", []))
    selected.symmetric_difference_update({value})
    await state.update_data(participant_ids=list(selected))
    visible_ids = [
        uuid.UUID(button.callback_data.split(":", 1)[1])
        for row in callback.message.reply_markup.inline_keyboard
        for button in row
        if button.callback_data and button.callback_data.startswith("evp:")
        and button.callback_data != "evp:done"
    ]
    async with SessionLocal() as session:
        users = list((await session.scalars(select(User).where(User.id.in_(visible_ids)))).all())
    by_id = {user.id: user for user in users}
    ordered = [by_id[item] for item in visible_ids if item in by_id]
    await callback.message.edit_reply_markup(
        reply_markup=event_people_keyboard(ordered, selected)
    )
    await callback.answer("Выбор обновлён")


async def create_event_from_state(
    message: Message,
    state: FSMContext,
    actor: User,
) -> None:
    data = await state.get_data()
    try:
        async with SessionLocal() as session:
            db_actor = await session.get(User, actor.id)
            assert db_actor
            event = await create_event(
                session,
                db_actor,
                EventCreate(
                    title=data["title"],
                    description=data.get("description"),
                    starts_at=datetime.fromisoformat(data["starts_at"]),
                    ends_at=(
                        datetime.fromisoformat(data["ends_at"])
                        if data.get("ends_at")
                        else None
                    ),
                    budget=data.get("budget"),
                    participant_ids=[
                        uuid.UUID(value) for value in data.get("participant_ids", [])
                    ],
                ),
            )
            await session.commit()
            event_id = event.id
        await state.clear()
        await message.answer("Мероприятие создано.")
        await show_event(message, actor, event_id)
    except Exception as exc:
        await answer_http_error(message, exc)


async def build_event_view(actor: User, event_id: uuid.UUID) -> tuple[str, InlineKeyboardMarkup]:
    async with SessionLocal() as session:
        event = await event_routes.event_for_manager(session, event_id, actor)
        participants = list(
            (
                await session.scalars(
                    select(User)
                    .join(EventParticipant)
                    .where(EventParticipant.event_id == event.id)
                )
            ).all()
        )
        task_count = await session.scalar(
            select(func.count()).select_from(Task).where(Task.event_id == event.id)
        )
    lines = [
        f"🗓 {event.title}",
        f"Начало: {format_dt(event.starts_at)}",
        f"Окончание: {format_dt(event.ends_at)}",
        f"Бюджет: {event.budget if event.budget is not None else 'не указан'}",
        f"Участников: {len(participants)}",
        f"Задач: {task_count or 0}",
    ]
    if event.description:
        lines.append(f"\n{event.description}")
    if event.retention_delete_at:
        lines.append(f"\nХранить до: {format_dt(event.retention_delete_at)}")
    if event.retention_extended_until:
        lines.append(f"Продлено до: {format_dt(event.retention_extended_until)}")
    rows = [
        [InlineKeyboardButton(text="📚 Архив", callback_data=f"earc:{event.id}")],
    ]
    if actor.role == Role.ADMIN and event.retention_delete_at:
        rows.append(
            [InlineKeyboardButton(text="🕒 Продлить хранение", callback_data=f"eret:{event.id}")]
        )
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=rows)


async def show_event(target: Message | CallbackQuery, actor: User, event_id: uuid.UUID) -> None:
    text, keyboard = await build_event_view(actor, event_id)
    if isinstance(target, CallbackQuery):
        try:
            await target.message.edit_text(text, reply_markup=keyboard)
        except TelegramBadRequest:
            await target.message.answer(text, reply_markup=keyboard)
        await target.answer()
    else:
        await target.answer(text, reply_markup=keyboard)


@router.callback_query(F.data.startswith("event:"))
async def event_detail(callback: CallbackQuery) -> None:
    if callback.data == "event:new":
        return
    actor = await sync_callback_user(callback)
    try:
        await show_event(callback, actor, uuid.UUID(callback.data.split(":", 1)[1]))
    except Exception as exc:
        await answer_http_error(callback, exc)


@router.callback_query(F.data.startswith("earc:"))
async def event_archive(callback: CallbackQuery) -> None:
    actor = await sync_callback_user(callback)
    event_id = uuid.UUID(callback.data.split(":", 1)[1])
    try:
        async with SessionLocal() as session:
            event = await event_routes.event_for_manager(session, event_id, actor)
            archive = await event_routes.archive_out(session, event)
        lines = [
            f"📚 Архив: {event.title}",
            f"Участников: {len(archive.participants)}",
            f"Задач: {len(archive.tasks)}",
            "",
        ]
        for task in archive.tasks[:25]:
            photos = task.report.photo_count if task.report else 0
            lines.append(
                f"• {task.title} — {STATUS_LABELS.get(task.status, task.status.value)}, фото: {photos}"
            )
        rows = [
            [
                InlineKeyboardButton(text="📄 PDF", callback_data=f"epdf:{event.id}"),
                InlineKeyboardButton(text="🗜 Фото ZIP", callback_data=f"ezip:{event.id}"),
            ],
            [InlineKeyboardButton(text="⬅️ Назад", callback_data=f"event:{event.id}")],
        ]
        await callback.message.edit_text(
            "\n".join(lines),
            reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
        )
        await callback.answer()
    except Exception as exc:
        await answer_http_error(callback, exc)


@router.callback_query(F.data.startswith("epdf:"))
async def event_pdf(callback: CallbackQuery) -> None:
    actor = await sync_callback_user(callback)
    event_id = uuid.UUID(callback.data.split(":", 1)[1])
    try:
        async with SessionLocal() as session:
            event = await event_routes.event_for_manager(session, event_id, actor)
            archive = await event_routes.archive_out(session, event)
            payload = build_event_archive_pdf(
                event,
                archive.participants,
                [task.model_dump() for task in archive.tasks],
            )
        await callback.message.answer_document(
            BufferedInputFile(payload, filename=f"event-{event.id}-archive.pdf")
        )
        await callback.answer()
    except Exception as exc:
        await answer_http_error(callback, exc)


@router.callback_query(F.data.startswith("ezip:"))
async def event_zip(callback: CallbackQuery) -> None:
    actor = await sync_callback_user(callback)
    event_id = uuid.UUID(callback.data.split(":", 1)[1])
    try:
        async with SessionLocal() as session:
            event = await event_routes.event_for_manager(session, event_id, actor)
            photos = list(
                (
                    await session.scalars(
                        select(TaskPhoto)
                        .join(TaskReport, TaskPhoto.report_id == TaskReport.id)
                        .join(Task, TaskReport.task_id == Task.id)
                        .where(Task.event_id == event.id)
                    )
                ).all()
            )
            payload = build_photo_zip(photos)
        await callback.message.answer_document(
            BufferedInputFile(payload, filename=f"event-{event.id}-photos.zip")
        )
        await callback.answer()
    except Exception as exc:
        await answer_http_error(callback, exc)


@router.callback_query(F.data.startswith("eret:"))
async def event_retention_start(callback: CallbackQuery, state: FSMContext) -> None:
    actor = await sync_callback_user(callback)
    if actor.role != Role.ADMIN:
        await callback.answer("Только администратор.", show_alert=True)
        return
    await state.set_state(RetentionFlow.until)
    await state.update_data(event_id=callback.data.split(":", 1)[1])
    await callback.message.answer("Новая дата хранения: ДД.ММ.ГГГГ ЧЧ:ММ.")
    await callback.answer()


@router.message(RetentionFlow.until, F.text)
async def event_retention_finish(message: Message, state: FSMContext) -> None:
    actor = await sync_user(message)
    data = await state.get_data()
    try:
        until = parse_input_datetime(message.text)
        event_id = uuid.UUID(data["event_id"])
        async with SessionLocal() as session:
            await event_routes.extend_event_retention(
                event_id,
                RetentionExtend(until=until),
                actor,
                session,
            )
        await state.clear()
        await message.answer("Срок хранения продлён.")
        await show_event(message, actor, event_id)
    except ValueError:
        await message.answer("Формат: ДД.ММ.ГГГГ ЧЧ:ММ.")
    except Exception as exc:
        await answer_http_error(message, exc)


# -------------------- Administration --------------------


@router.message(F.text == "⚙️ Администрирование")
async def administration(message: Message) -> None:
    actor = await sync_user(message)
    if actor.role != Role.ADMIN:
        await message.answer("Недостаточно прав.")
        return
    async with SessionLocal() as session:
        users = list((await session.scalars(select(User).order_by(User.full_name).limit(100))).all())
    rows = [
        [
            InlineKeyboardButton(
                text=f"{display_user(user)[:35]} · {ROLE_LABELS[user.role]}",
                callback_data=f"admuser:{user.id}",
            )
        ]
        for user in users[:40]
    ]
    await message.answer(
        "⚙️ Управление пользователями",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
    )


@router.callback_query(F.data.startswith("admuser:"))
async def admin_user_detail(callback: CallbackQuery) -> None:
    actor = await sync_callback_user(callback)
    if actor.role != Role.ADMIN:
        await callback.answer("Недостаточно прав.", show_alert=True)
        return
    user_id = uuid.UUID(callback.data.split(":", 1)[1])
    async with SessionLocal() as session:
        user = await session.get(User, user_id)
        sector = await session.get(Sector, user.sector_id) if user and user.sector_id else None
    if not user:
        await callback.answer("Пользователь не найден.", show_alert=True)
        return
    text = "\n".join(
        [
            f"👤 {user.full_name or 'Без имени'}",
            f"@{user.telegram_username}" if user.telegram_username else "username не указан",
            f"Роль: {ROLE_LABELS[user.role]}",
            f"Статус: {user.status.value}",
            f"Сектор: {sector.name if sector else 'не назначен'}",
        ]
    )
    rows = [
        [
            InlineKeyboardButton(text="Участник", callback_data=f"urol:p:{user.id}"),
            InlineKeyboardButton(text="Председатель", callback_data=f"urol:h:{user.id}"),
            InlineKeyboardButton(text="Админ", callback_data=f"urol:a:{user.id}"),
        ],
        [
            InlineKeyboardButton(
                text="Активировать" if user.status == UserStatus.INACTIVE else "Деактивировать",
                callback_data=f"ustat:{user.id}",
            )
        ],
    ]
    await callback.message.edit_text(
        text,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("urol:"))
async def admin_user_role(callback: CallbackQuery) -> None:
    actor = await sync_callback_user(callback)
    _, code, raw_id = callback.data.split(":", 2)
    role = {"p": Role.PARTICIPANT, "h": Role.SECTOR_HEAD, "a": Role.ADMIN}.get(code)
    if role is None:
        await callback.answer("Некорректная роль.", show_alert=True)
        return
    user_id = uuid.UUID(raw_id)
    try:
        async with SessionLocal() as session:
            await admin_routes.update_user(
                user_id,
                UserAdminUpdate(role=role),
                actor,
                session,
            )
        await callback.answer("Роль изменена")
        await callback.message.edit_text(
            "Роль пользователя изменена.",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [InlineKeyboardButton(text="👤 Открыть карточку", callback_data=f"admuser:{user_id}")]
                ]
            ),
        )
    except Exception as exc:
        await answer_http_error(callback, exc)


@router.callback_query(F.data.startswith("ustat:"))
async def admin_user_status(callback: CallbackQuery) -> None:
    actor = await sync_callback_user(callback)
    user_id = uuid.UUID(callback.data.split(":", 1)[1])
    try:
        async with SessionLocal() as session:
            user = await session.get(User, user_id)
            if not user:
                raise HTTPException(status_code=404, detail="Пользователь не найден")
            new_status = (
                UserStatus.ACTIVE
                if user.status == UserStatus.INACTIVE
                else UserStatus.INACTIVE
            )
            await admin_routes.update_user(
                user_id,
                UserAdminUpdate(status=new_status),
                actor,
                session,
            )
        await callback.answer("Статус изменён")
        await callback.message.edit_text(
            "Статус пользователя изменён.",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [InlineKeyboardButton(text="👤 Открыть карточку", callback_data=f"admuser:{user_id}")]
                ]
            ),
        )
    except Exception as exc:
        await answer_http_error(callback, exc)


# -------------------- Membership synchronization and runner --------------------


@router.chat_member()
async def membership_changed(update: ChatMemberUpdated) -> None:
    async with SessionLocal() as session:
        legacy_id = legacy_mtproto_channel_id(update.chat.id)
        task_chat = await session.scalar(
            select(TaskChat).where(
                or_(
                    TaskChat.telegram_chat_id == update.chat.id,
                    TaskChat.telegram_chat_id == legacy_id,
                )
            )
        )
        if not task_chat:
            return
        if task_chat.telegram_chat_id != update.chat.id:
            task_chat.telegram_chat_id = update.chat.id
        user = await session.scalar(
            select(User).where(User.telegram_id == update.new_chat_member.user.id)
        )
        if not user:
            await session.commit()
            return
        member = await session.scalar(
            select(TaskChatMember).where(
                TaskChatMember.task_chat_id == task_chat.id,
                TaskChatMember.user_id == user.id,
            )
        )
        if not member:
            await session.commit()
            return
        joined_statuses = {"member", "administrator", "creator", "owner"}
        if update.new_chat_member.status in joined_statuses:
            member.state = MembershipState.JOINED
            member.joined_at = member.joined_at or datetime.now(UTC)
            member.last_checked_at = datetime.now(UTC)
            member.next_reminder_at = None
            member.last_error = None
        else:
            still_assigned = await session.scalar(
                select(TaskMember.id).where(
                    TaskMember.task_id == task_chat.task_id,
                    TaskMember.user_id == user.id,
                )
            )
            if still_assigned:
                member.state = MembershipState.NOT_JOINED
                member.last_checked_at = datetime.now(UTC)
                session.add(
                    OutboxEvent(
                        event_type="TASK_CHAT_MEMBER_INVITE_REQUESTED",
                        aggregate_type="task_chat",
                        aggregate_id=str(task_chat.id),
                        payload={
                            "task_id": str(task_chat.task_id),
                            "user_id": str(user.id),
                        },
                    )
                )
            else:
                member.state = MembershipState.REMOVED
                member.next_reminder_at = None
        await session.commit()


async def run() -> None:
    settings = get_settings()
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    if not token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is required")
    dispatcher = Dispatcher()
    dispatcher.include_router(router)
    if settings.telegram_webhook_url:
        bot = build_telegram_bot(token)
        path = settings.telegram_webhook_path
        if not path.startswith("/"):
            path = f"/{path}"
        await bot.set_webhook(
            url=f"{settings.telegram_webhook_url.rstrip('/')}{path}",
            secret_token=settings.telegram_webhook_secret or None,
            allowed_updates=["message", "callback_query", "chat_member"],
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
                allowed_updates=["message", "callback_query", "chat_member"],
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
