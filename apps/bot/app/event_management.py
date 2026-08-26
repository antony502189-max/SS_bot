import base64
import uuid

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from fastapi import HTTPException
from sqlalchemy import func, select

from apps.api.app.db import SessionLocal
from apps.api.app.models import Event, EventParticipant, Role, Sector, Task, User
from apps.api.app.routers import events as event_routes
from apps.api.app.schemas import EventParticipantCreate, EventUpdate
from apps.bot.app.main import (
    answer_http_error,
    display_user,
    format_dt,
    main_keyboard,
    parse_input_datetime,
    search_users,
    sync_callback_user,
    sync_user,
)

router = Router(name="bot_event_management")


class EventEditFlow(StatesGroup):
    value = State()


class EventParticipantFlow(StatesGroup):
    query = State()


def pack_uuid(value: uuid.UUID) -> str:
    return base64.urlsafe_b64encode(value.bytes).decode().rstrip("=")


def unpack_uuid(value: str) -> uuid.UUID:
    return uuid.UUID(bytes=base64.urlsafe_b64decode(value + "=" * (-len(value) % 4)))


async def event_for_actor(event_id: uuid.UUID, actor: User) -> Event:
    async with SessionLocal() as session:
        return await event_routes.event_for_manager(session, event_id, actor)


async def render_event(callback: CallbackQuery, actor: User, event_id: uuid.UUID) -> None:
    async with SessionLocal() as session:
        event = await event_routes.event_for_manager(session, event_id, actor)
        participant_count = await session.scalar(
            select(func.count())
            .select_from(EventParticipant)
            .where(EventParticipant.event_id == event.id)
        )
        task_count = await session.scalar(
            select(func.count()).select_from(Task).where(Task.event_id == event.id)
        )
        sector = await session.get(Sector, event.sector_id) if event.sector_id else None
    token = pack_uuid(event.id)
    lines = [
        f"🗓 {event.title}",
        f"Начало: {format_dt(event.starts_at)}",
        f"Окончание: {format_dt(event.ends_at)}",
        f"Бюджет: {event.budget if event.budget is not None else 'не указан'}",
        f"Сектор: {sector.name if sector else 'не назначен'}",
        f"Участников: {participant_count or 0}",
        f"Задач: {task_count or 0}",
    ]
    if event.description:
        lines.append(f"\n{event.description}")
    if event.retention_delete_at:
        lines.append(f"\nХранить до: {format_dt(event.retention_delete_at)}")
    if event.retention_extended_until:
        lines.append(f"Продлено до: {format_dt(event.retention_extended_until)}")

    rows = [
        [InlineKeyboardButton(text="✏️ Редактировать", callback_data=f"evedit:{token}")],
        [InlineKeyboardButton(text="👥 Участники", callback_data=f"evmembers:{token}")],
        [InlineKeyboardButton(text="📚 Архив", callback_data=f"earc:{event.id}")],
    ]
    if actor.role == Role.ADMIN:
        rows.append([InlineKeyboardButton(text="🏢 Сектор", callback_data=f"evsector:{token}")])
        if event.retention_delete_at:
            rows.append(
                [InlineKeyboardButton(text="🕒 Продлить хранение", callback_data=f"eret:{event.id}")]
            )
    rows.append([InlineKeyboardButton(text="⬅️ Мероприятия", callback_data="events:list")])
    await callback.message.edit_text(
        "\n".join(lines),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
    )


@router.callback_query(F.data == "events:list")
async def event_list_callback(callback: CallbackQuery) -> None:
    actor = await sync_callback_user(callback)
    if actor.role not in {Role.ADMIN, Role.SECTOR_HEAD}:
        await callback.answer("Недостаточно прав.", show_alert=True)
        return
    async with SessionLocal() as session:
        statement = select(Event).where(Event.purged_at.is_(None)).order_by(Event.starts_at.desc())
        if actor.role == Role.SECTOR_HEAD:
            statement = statement.where(Event.sector_id == actor.sector_id)
        events = list((await session.scalars(statement.limit(40))).all())
    rows = [[InlineKeyboardButton(text="➕ Новое мероприятие", callback_data="event:new")]]
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
    await callback.message.edit_text(
        "🗓 Мероприятия",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("event:"), F.data != "event:new")
async def event_detail(callback: CallbackQuery) -> None:
    actor = await sync_callback_user(callback)
    try:
        await render_event(callback, actor, uuid.UUID(callback.data.split(":", 1)[1]))
        await callback.answer()
    except Exception as exc:
        await answer_http_error(callback, exc)


@router.callback_query(F.data.startswith("evedit:"))
async def event_edit_menu(callback: CallbackQuery) -> None:
    actor = await sync_callback_user(callback)
    event_id = unpack_uuid(callback.data.split(":", 1)[1])
    try:
        await event_for_actor(event_id, actor)
        token = pack_uuid(event_id)
        rows = [
            [InlineKeyboardButton(text="Название", callback_data=f"evfield:t:{token}")],
            [InlineKeyboardButton(text="Начало", callback_data=f"evfield:s:{token}")],
            [InlineKeyboardButton(text="Окончание", callback_data=f"evfield:e:{token}")],
            [InlineKeyboardButton(text="Бюджет", callback_data=f"evfield:b:{token}")],
            [InlineKeyboardButton(text="Описание", callback_data=f"evfield:d:{token}")],
            [InlineKeyboardButton(text="⬅️ Назад", callback_data=f"event:{event_id}")],
        ]
        await callback.message.edit_text(
            "Что изменить?",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
        )
        await callback.answer()
    except Exception as exc:
        await answer_http_error(callback, exc)


@router.callback_query(F.data.startswith("evfield:"))
async def event_edit_start(callback: CallbackQuery, state: FSMContext) -> None:
    actor = await sync_callback_user(callback)
    _, field, raw_event = callback.data.split(":", 2)
    event_id = unpack_uuid(raw_event)
    try:
        await event_for_actor(event_id, actor)
        prompts = {
            "t": "Новое название мероприятия:",
            "s": "Новое начало: ДД.ММ.ГГГГ ЧЧ:ММ.",
            "e": "Новое окончание: ДД.ММ.ГГГГ ЧЧ:ММ или «-».",
            "b": "Новый бюджет числом или «-».",
            "d": "Новое описание или «-».",
        }
        if field not in prompts:
            raise HTTPException(status_code=422, detail="Неизвестное поле")
        await state.set_state(EventEditFlow.value)
        await state.update_data(event_edit_id=str(event_id), event_edit_field=field)
        await callback.message.answer(f"{prompts[field]} Для отмены: /cancel")
        await callback.answer()
    except Exception as exc:
        await answer_http_error(callback, exc)


@router.message(EventEditFlow.value, Command("cancel"))
async def event_edit_cancel(message: Message, state: FSMContext) -> None:
    actor = await sync_user(message)
    await state.clear()
    await message.answer("Редактирование отменено.", reply_markup=main_keyboard(actor))


@router.message(EventEditFlow.value, F.text)
async def event_edit_finish(message: Message, state: FSMContext) -> None:
    actor = await sync_user(message)
    data = await state.get_data()
    event_id = uuid.UUID(data["event_edit_id"])
    field = data["event_edit_field"]
    raw = message.text.strip()
    try:
        if field == "t":
            if not 2 <= len(raw) <= 200:
                raise ValueError("Название должно содержать от 2 до 200 символов.")
            update = EventUpdate(title=raw)
        elif field == "s":
            update = EventUpdate(starts_at=parse_input_datetime(raw))
        elif field == "e":
            update = EventUpdate(ends_at=None if raw == "-" else parse_input_datetime(raw))
        elif field == "b":
            value = None if raw == "-" else float(raw.replace(",", "."))
            if value is not None and value < 0:
                raise ValueError("Бюджет не может быть отрицательным.")
            update = EventUpdate(budget=value)
        elif field == "d":
            if len(raw) > 5000:
                raise ValueError("Описание: максимум 5000 символов.")
            update = EventUpdate(description=None if raw == "-" else raw)
        else:
            raise ValueError("Неизвестное поле.")
        async with SessionLocal() as session:
            await event_routes.update_event(event_id, update, actor, session)
        await state.clear()
        await message.answer(
            "Мероприятие обновлено.",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [InlineKeyboardButton(text="🗓 Открыть", callback_data=f"event:{event_id}")]
                ]
            ),
        )
    except (ValueError, TypeError) as exc:
        await message.answer(str(exc) or "Проверьте введённое значение.")
    except Exception as exc:
        await answer_http_error(message, exc)


@router.callback_query(F.data.startswith("evmembers:"))
async def event_members(callback: CallbackQuery) -> None:
    actor = await sync_callback_user(callback)
    event_id = unpack_uuid(callback.data.split(":", 1)[1])
    try:
        async with SessionLocal() as session:
            event = await event_routes.event_for_manager(session, event_id, actor)
            participants = list(
                (
                    await session.scalars(
                        select(User)
                        .join(EventParticipant)
                        .where(EventParticipant.event_id == event.id)
                        .order_by(User.full_name)
                    )
                ).all()
            )
        event_token = pack_uuid(event.id)
        lines = [f"👥 Участники: {event.title}", f"Всего: {len(participants)}"]
        rows = []
        for participant in participants[:35]:
            rows.append(
                [
                    InlineKeyboardButton(
                        text=f"➖ {display_user(participant)[:34]}",
                        callback_data=(
                            f"evrm:{event_token}:{pack_uuid(participant.id)}"
                        ),
                    )
                ]
            )
        rows.extend(
            [
                [InlineKeyboardButton(text="➕ Добавить", callback_data=f"evaddstart:{event_token}")],
                [InlineKeyboardButton(text="⬅️ Назад", callback_data=f"event:{event.id}")],
            ]
        )
        await callback.message.edit_text(
            "\n".join(lines),
            reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
        )
        await callback.answer()
    except Exception as exc:
        await answer_http_error(callback, exc)


@router.callback_query(F.data.startswith("evaddstart:"))
async def event_member_add_start(callback: CallbackQuery, state: FSMContext) -> None:
    actor = await sync_callback_user(callback)
    event_id = unpack_uuid(callback.data.split(":", 1)[1])
    try:
        await event_for_actor(event_id, actor)
        await state.set_state(EventParticipantFlow.query)
        await state.update_data(event_participant_event_id=str(event_id))
        await callback.message.answer("Введите ФИО или @username нового участника. /cancel — отмена")
        await callback.answer()
    except Exception as exc:
        await answer_http_error(callback, exc)


@router.message(EventParticipantFlow.query, Command("cancel"))
async def event_member_add_cancel(message: Message, state: FSMContext) -> None:
    actor = await sync_user(message)
    await state.clear()
    await message.answer("Добавление участника отменено.", reply_markup=main_keyboard(actor))


@router.message(EventParticipantFlow.query, F.text)
async def event_member_add_search(message: Message, state: FSMContext) -> None:
    actor = await sync_user(message)
    data = await state.get_data()
    event_id = uuid.UUID(data["event_participant_event_id"])
    try:
        await event_for_actor(event_id, actor)
        users = await search_users(actor, message.text)
        if not users:
            await message.answer("Никого не найдено. Измените запрос.")
            return
        event_token = pack_uuid(event_id)
        rows = [
            [
                InlineKeyboardButton(
                    text=f"➕ {display_user(user)}",
                    callback_data=f"evadd:{event_token}:{pack_uuid(user.id)}",
                )
            ]
            for user in users[:10]
        ]
        await message.answer(
            "Кого добавить?",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
        )
    except Exception as exc:
        await answer_http_error(message, exc)


@router.callback_query(F.data.startswith("evadd:"))
async def event_member_add(callback: CallbackQuery, state: FSMContext) -> None:
    actor = await sync_callback_user(callback)
    _, raw_event, raw_user = callback.data.split(":", 2)
    event_id = unpack_uuid(raw_event)
    user_id = unpack_uuid(raw_user)
    try:
        async with SessionLocal() as session:
            await event_routes.add_participant(
                event_id,
                EventParticipantCreate(user_id=user_id),
                actor,
                session,
            )
        await state.clear()
        await callback.answer("Участник добавлен")
        token = pack_uuid(event_id)
        await callback.message.edit_text(
            "Участник добавлен в мероприятие.",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [InlineKeyboardButton(text="👥 К участникам", callback_data=f"evmembers:{token}")]
                ]
            ),
        )
    except Exception as exc:
        await answer_http_error(callback, exc)


@router.callback_query(F.data.startswith("evrm:"))
async def event_member_remove(callback: CallbackQuery) -> None:
    actor = await sync_callback_user(callback)
    _, raw_event, raw_user = callback.data.split(":", 2)
    event_id = unpack_uuid(raw_event)
    user_id = unpack_uuid(raw_user)
    try:
        async with SessionLocal() as session:
            await event_routes.remove_participant(event_id, user_id, actor, session)
        await callback.answer("Участник удалён")
        token = pack_uuid(event_id)
        await callback.message.edit_text(
            "Участник удалён из мероприятия.",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [InlineKeyboardButton(text="👥 К участникам", callback_data=f"evmembers:{token}")]
                ]
            ),
        )
    except Exception as exc:
        await answer_http_error(callback, exc)


@router.callback_query(F.data.startswith("evsector:"))
async def event_sector_choose(callback: CallbackQuery) -> None:
    actor = await sync_callback_user(callback)
    if actor.role != Role.ADMIN:
        await callback.answer("Только администратор.", show_alert=True)
        return
    event_id = unpack_uuid(callback.data.split(":", 1)[1])
    try:
        async with SessionLocal() as session:
            event = await event_routes.event_for_manager(session, event_id, actor)
            task_count = await session.scalar(
                select(func.count()).select_from(Task).where(Task.event_id == event.id)
            )
            sectors = list(
                (
                    await session.scalars(
                        select(Sector).where(Sector.is_active.is_(True)).order_by(Sector.name)
                    )
                ).all()
            )
        if task_count:
            await callback.answer(
                "Сектор нельзя менять после появления задач в мероприятии.", show_alert=True
            )
            return
        event_token = pack_uuid(event_id)
        rows = [
            [
                InlineKeyboardButton(
                    text=f"🏢 {sector.name}",
                    callback_data=f"evsetsec:{event_token}:{pack_uuid(sector.id)}",
                )
            ]
            for sector in sectors[:40]
        ]
        rows.append(
            [InlineKeyboardButton(text="🚫 Без сектора", callback_data=f"evsetsec:{event_token}:none")]
        )
        rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data=f"event:{event_id}")])
        await callback.message.edit_text(
            "Выберите сектор мероприятия:",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
        )
        await callback.answer()
    except Exception as exc:
        await answer_http_error(callback, exc)


@router.callback_query(F.data.startswith("evsetsec:"))
async def event_sector_set(callback: CallbackQuery) -> None:
    actor = await sync_callback_user(callback)
    if actor.role != Role.ADMIN:
        await callback.answer("Только администратор.", show_alert=True)
        return
    _, raw_event, raw_sector = callback.data.split(":", 2)
    event_id = unpack_uuid(raw_event)
    sector_id = None if raw_sector == "none" else unpack_uuid(raw_sector)
    try:
        async with SessionLocal() as session:
            task_count = await session.scalar(
                select(func.count()).select_from(Task).where(Task.event_id == event_id)
            )
            if task_count:
                raise HTTPException(
                    status_code=422,
                    detail="Сектор нельзя менять после появления задач в мероприятии",
                )
            if sector_id:
                sector = await session.get(Sector, sector_id)
                if not sector or not sector.is_active:
                    raise HTTPException(status_code=422, detail="Сектор недоступен")
            await event_routes.update_event(
                event_id,
                EventUpdate(sector_id=sector_id),
                actor,
                session,
            )
        await render_event(callback, actor, event_id)
        await callback.answer("Сектор сохранён")
    except Exception as exc:
        await answer_http_error(callback, exc)
