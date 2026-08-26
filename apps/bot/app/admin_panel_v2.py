import uuid

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from fastapi import HTTPException
from sqlalchemy import func, select

from apps.api.app.db import SessionLocal
from apps.api.app.models import Role, Sector, User, UserStatus
from apps.api.app.routers import admin as admin_routes
from apps.api.app.routers import sectors as sector_routes
from apps.api.app.schemas import SectorCreate, SectorUpdate, UserAdminUpdate
from apps.bot.app.main import (
    ROLE_LABELS,
    answer_http_error,
    display_user,
    main_keyboard,
    sync_callback_user,
    sync_user,
)

router = Router(name="bot_admin_panel_v2")


class SectorCreateFlow(StatesGroup):
    name = State()
    description = State()


class SectorEditFlow(StatesGroup):
    name = State()
    description = State()


async def require_admin_message(message: Message) -> User | None:
    actor = await sync_user(message)
    if actor.role != Role.ADMIN:
        await message.answer("Недостаточно прав.", reply_markup=main_keyboard(actor))
        return None
    return actor


async def require_admin_callback(callback: CallbackQuery) -> User | None:
    actor = await sync_callback_user(callback)
    if actor.role != Role.ADMIN:
        await callback.answer("Недостаточно прав.", show_alert=True)
        return None
    return actor


def admin_home_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="👥 Пользователи", callback_data="admin:users")],
            [InlineKeyboardButton(text="🏢 Сектора", callback_data="admin:sectors")],
        ]
    )


async def render_user_card(callback: CallbackQuery, user_id: uuid.UUID) -> bool:
    async with SessionLocal() as session:
        user = await session.get(User, user_id)
        sector = await session.get(Sector, user.sector_id) if user and user.sector_id else None
    if not user:
        await callback.answer("Пользователь не найден.", show_alert=True)
        return False
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
        [InlineKeyboardButton(text="🏢 Назначить сектор", callback_data=f"usector:{user.id}")],
        [InlineKeyboardButton(text="⬅️ Пользователи", callback_data="admin:users")],
    ]
    await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
    return True


async def render_sector_card(callback: CallbackQuery, sector_id: uuid.UUID) -> bool:
    async with SessionLocal() as session:
        sector = await session.get(Sector, sector_id)
        user_count = await session.scalar(
            select(func.count()).select_from(User).where(User.sector_id == sector_id)
        )
    if not sector:
        await callback.answer("Сектор не найден.", show_alert=True)
        return False
    text = "\n".join(
        [
            f"🏢 {sector.name}",
            f"Статус: {'активен' if sector.is_active else 'отключён'}",
            f"Пользователей: {user_count or 0}",
            f"Описание: {sector.description or 'не указано'}",
        ]
    )
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="✏️ Переименовать", callback_data=f"secrename:{sector.id}")],
            [InlineKeyboardButton(text="📝 Описание", callback_data=f"secdesc:{sector.id}")],
            [
                InlineKeyboardButton(
                    text="✅ Активировать" if not sector.is_active else "⛔ Деактивировать",
                    callback_data=f"secstate:{sector.id}",
                )
            ],
            [InlineKeyboardButton(text="⬅️ Сектора", callback_data="admin:sectors")],
        ]
    )
    await callback.message.edit_text(text, reply_markup=keyboard)
    return True


@router.message(F.text == "⚙️ Администрирование")
async def administration_home(message: Message) -> None:
    if not await require_admin_message(message):
        return
    await message.answer("⚙️ Администрирование", reply_markup=admin_home_keyboard())


@router.callback_query(F.data == "admin:home")
async def administration_home_callback(callback: CallbackQuery) -> None:
    if not await require_admin_callback(callback):
        return
    await callback.message.edit_text("⚙️ Администрирование", reply_markup=admin_home_keyboard())
    await callback.answer()


@router.callback_query(F.data == "admin:users")
async def administration_users(callback: CallbackQuery) -> None:
    if not await require_admin_callback(callback):
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
    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="admin:home")])
    await callback.message.edit_text(
        "👥 Управление пользователями",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("admuser:"))
async def administration_user_card(callback: CallbackQuery) -> None:
    if not await require_admin_callback(callback):
        return
    user_id = uuid.UUID(callback.data.split(":", 1)[1])
    if await render_user_card(callback, user_id):
        await callback.answer()


@router.callback_query(F.data.startswith("usector:"))
async def administration_choose_user_sector(callback: CallbackQuery, state: FSMContext) -> None:
    if not await require_admin_callback(callback):
        return
    user_id = uuid.UUID(callback.data.split(":", 1)[1])
    async with SessionLocal() as session:
        user = await session.get(User, user_id)
        sectors = list(
            (
                await session.scalars(
                    select(Sector).where(Sector.is_active.is_(True)).order_by(Sector.name)
                )
            ).all()
        )
    if not user:
        await callback.answer("Пользователь не найден.", show_alert=True)
        return
    await state.update_data(admin_sector_user_id=str(user.id))
    rows = [
        [InlineKeyboardButton(text=f"🏢 {sector.name}", callback_data=f"asetsec:{sector.id}")]
        for sector in sectors[:45]
    ]
    rows.extend(
        [
            [InlineKeyboardButton(text="🚫 Без сектора", callback_data="asetsec:none")],
            [InlineKeyboardButton(text="⬅️ Назад", callback_data=f"admuser:{user.id}")],
        ]
    )
    await callback.message.edit_text(
        f"Выберите сектор для {display_user(user)}:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("asetsec:"))
async def administration_set_user_sector(callback: CallbackQuery, state: FSMContext) -> None:
    actor = await require_admin_callback(callback)
    if not actor:
        return
    state_data = await state.get_data()
    raw_user_id = state_data.get("admin_sector_user_id")
    if not raw_user_id:
        await callback.answer(
            "Сессия выбора сектора истекла. Откройте карточку заново.", show_alert=True
        )
        return
    user_id = uuid.UUID(raw_user_id)
    raw_sector_id = callback.data.split(":", 1)[1]
    sector_id = None if raw_sector_id == "none" else uuid.UUID(raw_sector_id)
    try:
        async with SessionLocal() as session:
            if sector_id:
                sector = await session.get(Sector, sector_id)
                if not sector or not sector.is_active:
                    raise HTTPException(status_code=422, detail="Сектор недоступен")
            await admin_routes.update_user(
                user_id,
                UserAdminUpdate(sector_id=sector_id),
                actor,
                session,
            )
        await state.update_data(admin_sector_user_id=None)
        await render_user_card(callback, user_id)
        await callback.answer("Сектор сохранён")
    except Exception as exc:
        await answer_http_error(callback, exc)


@router.callback_query(F.data == "admin:sectors")
async def administration_sectors(callback: CallbackQuery) -> None:
    if not await require_admin_callback(callback):
        return
    async with SessionLocal() as session:
        sectors = list((await session.scalars(select(Sector).order_by(Sector.name))).all())
    rows = [
        [
            InlineKeyboardButton(
                text=f"{'✅' if sector.is_active else '⛔'} {sector.name}",
                callback_data=f"sector:{sector.id}",
            )
        ]
        for sector in sectors[:45]
    ]
    rows.extend(
        [
            [InlineKeyboardButton(text="➕ Создать сектор", callback_data="secnew")],
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="admin:home")],
        ]
    )
    await callback.message.edit_text(
        "🏢 Сектора",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("sector:"))
async def administration_sector_card(callback: CallbackQuery) -> None:
    if not await require_admin_callback(callback):
        return
    sector_id = uuid.UUID(callback.data.split(":", 1)[1])
    if await render_sector_card(callback, sector_id):
        await callback.answer()


async def cancel_sector_flow(message: Message, state: FSMContext) -> None:
    actor = await sync_user(message)
    await state.clear()
    await message.answer("Операция с сектором отменена.", reply_markup=main_keyboard(actor))


@router.message(SectorCreateFlow.name, Command("cancel"))
@router.message(SectorCreateFlow.description, Command("cancel"))
@router.message(SectorEditFlow.name, Command("cancel"))
@router.message(SectorEditFlow.description, Command("cancel"))
async def administration_sector_cancel(message: Message, state: FSMContext) -> None:
    await cancel_sector_flow(message, state)


@router.callback_query(F.data == "secnew")
async def administration_sector_create_start(callback: CallbackQuery, state: FSMContext) -> None:
    if not await require_admin_callback(callback):
        return
    await state.set_state(SectorCreateFlow.name)
    await callback.message.answer("Введите название нового сектора. Для отмены: /cancel")
    await callback.answer()


@router.message(SectorCreateFlow.name, F.text)
async def administration_sector_create_name(message: Message, state: FSMContext) -> None:
    if not await require_admin_message(message):
        await state.clear()
        return
    name = message.text.strip()
    if len(name) < 2 or len(name) > 120:
        await message.answer("Название должно содержать от 2 до 120 символов.")
        return
    await state.update_data(sector_name=name)
    await state.set_state(SectorCreateFlow.description)
    await message.answer("Введите описание сектора или отправьте «-», если оно не нужно.")


@router.message(SectorCreateFlow.description, F.text)
async def administration_sector_create_finish(message: Message, state: FSMContext) -> None:
    actor = await require_admin_message(message)
    if not actor:
        await state.clear()
        return
    data = await state.get_data()
    description = message.text.strip()
    if len(description) > 2000:
        await message.answer("Описание слишком длинное. Максимум 2000 символов.")
        return
    try:
        async with SessionLocal() as session:
            sector = await sector_routes.create_sector(
                SectorCreate(
                    name=data["sector_name"],
                    description=None if description == "-" else description,
                ),
                actor,
                session,
            )
        await state.clear()
        await message.answer(
            f"Сектор «{sector.name}» создан.",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [InlineKeyboardButton(text="🏢 Открыть сектор", callback_data=f"sector:{sector.id}")]
                ]
            ),
        )
    except Exception as exc:
        await answer_http_error(message, exc)


@router.callback_query(F.data.startswith("secrename:"))
async def administration_sector_rename_start(callback: CallbackQuery, state: FSMContext) -> None:
    if not await require_admin_callback(callback):
        return
    await state.set_state(SectorEditFlow.name)
    await state.update_data(sector_edit_id=callback.data.split(":", 1)[1])
    await callback.message.answer("Введите новое название сектора. Для отмены: /cancel")
    await callback.answer()


@router.message(SectorEditFlow.name, F.text)
async def administration_sector_rename_finish(message: Message, state: FSMContext) -> None:
    actor = await require_admin_message(message)
    if not actor:
        await state.clear()
        return
    name = message.text.strip()
    if len(name) < 2 or len(name) > 120:
        await message.answer("Название должно содержать от 2 до 120 символов.")
        return
    data = await state.get_data()
    sector_id = uuid.UUID(data["sector_edit_id"])
    try:
        async with SessionLocal() as session:
            sector = await sector_routes.update_sector(
                sector_id,
                SectorUpdate(name=name),
                actor,
                session,
            )
        await state.clear()
        await message.answer(
            "Название изменено.",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [InlineKeyboardButton(text="🏢 Открыть сектор", callback_data=f"sector:{sector.id}")]
                ]
            ),
        )
    except Exception as exc:
        await answer_http_error(message, exc)


@router.callback_query(F.data.startswith("secdesc:"))
async def administration_sector_description_start(callback: CallbackQuery, state: FSMContext) -> None:
    if not await require_admin_callback(callback):
        return
    await state.set_state(SectorEditFlow.description)
    await state.update_data(sector_edit_id=callback.data.split(":", 1)[1])
    await callback.message.answer("Введите новое описание или «-», чтобы очистить его. /cancel — отмена")
    await callback.answer()


@router.message(SectorEditFlow.description, F.text)
async def administration_sector_description_finish(message: Message, state: FSMContext) -> None:
    actor = await require_admin_message(message)
    if not actor:
        await state.clear()
        return
    description = message.text.strip()
    if len(description) > 2000:
        await message.answer("Описание слишком длинное. Максимум 2000 символов.")
        return
    data = await state.get_data()
    sector_id = uuid.UUID(data["sector_edit_id"])
    try:
        async with SessionLocal() as session:
            sector = await sector_routes.update_sector(
                sector_id,
                SectorUpdate(description=None if description == "-" else description),
                actor,
                session,
            )
        await state.clear()
        await message.answer(
            "Описание изменено.",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [InlineKeyboardButton(text="🏢 Открыть сектор", callback_data=f"sector:{sector.id}")]
                ]
            ),
        )
    except Exception as exc:
        await answer_http_error(message, exc)


@router.callback_query(F.data.startswith("secstate:"))
async def administration_sector_state(callback: CallbackQuery) -> None:
    actor = await require_admin_callback(callback)
    if not actor:
        return
    sector_id = uuid.UUID(callback.data.split(":", 1)[1])
    try:
        async with SessionLocal() as session:
            sector = await session.get(Sector, sector_id)
            if not sector:
                raise HTTPException(status_code=404, detail="Сектор не найден")
            await sector_routes.update_sector(
                sector_id,
                SectorUpdate(is_active=not sector.is_active),
                actor,
                session,
            )
        await render_sector_card(callback, sector_id)
        await callback.answer("Статус сектора изменён")
    except Exception as exc:
        await answer_http_error(callback, exc)
