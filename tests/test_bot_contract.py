import uuid
from datetime import UTC, datetime, timedelta

import pytest

from apps.api.app.models import (
    Role,
    Task,
    TaskChecklistItem,
    TaskKind,
    TaskMember,
    User,
    UserStatus,
)
from apps.bot.app.main import menu_keyboard, people_keyboard, task_card


def keyboard_labels(role: Role) -> list[str]:
    return [button.text for row in menu_keyboard(role).keyboard for button in row]


def test_role_aware_home_menu_exposes_only_authorized_actions() -> None:
    assert keyboard_labels(Role.PARTICIPANT) == ["📋 Мои задачи", "👤 Профиль", "ℹ️ Помощь"]
    assert keyboard_labels(Role.SECTOR_HEAD) == [
        "📋 Мои задачи",
        "👤 Профиль",
        "➕ Создать задачу",
        "🗓 События",
        "ℹ️ Помощь",
    ]
    assert keyboard_labels(Role.ADMIN)[-2:] == ["⚙️ Администрирование", "ℹ️ Помощь"]


def test_task_selection_callback_data_fits_telegram_limit() -> None:
    user = User(
        id=uuid.uuid4(),
        telegram_id=710,
        full_name="Тестовый Пользователь",
        status=UserStatus.ACTIVE,
    )
    markup = people_keyboard([user], set())
    callback_data = markup.inline_keyboard[0][0].callback_data
    assert callback_data is not None
    assert len(callback_data.encode()) < 64


@pytest.mark.asyncio
async def test_manager_task_card_exposes_all_checklist_edits_with_valid_callbacks(session) -> None:
    manager = User(
        telegram_id=713,
        full_name="Администратор Тест",
        normalized_full_name="администратор тест",
        role=Role.ADMIN,
        status=UserStatus.ACTIVE,
    )
    participant = User(
        telegram_id=714,
        full_name="Участник Тест",
        normalized_full_name="участник тест",
        status=UserStatus.ACTIVE,
    )
    session.add_all([manager, participant])
    await session.flush()
    task = Task(
        title="Групповая задача",
        kind=TaskKind.GROUP,
        deadline=datetime.now(UTC) + timedelta(days=1),
        creator_id=manager.id,
        leader_id=manager.id,
        idempotency_key="task-card-controls",
    )
    session.add(task)
    await session.flush()
    session.add_all(
        [
            TaskMember(task_id=task.id, user_id=manager.id, is_creator=True, is_leader=True),
            TaskMember(task_id=task.id, user_id=participant.id),
            TaskChecklistItem(task_id=task.id, title="Проверить зал", position=0),
        ]
    )
    await session.commit()

    _, markup = await task_card(session, task, manager)
    callbacks = [button.callback_data for row in markup.inline_keyboard for button in row]

    assert f"task:members:{task.id}" in callbacks
    assert any(value and value.startswith("task:checkedit:") for value in callbacks)
    assert any(value and value.startswith("task:checkremove:") for value in callbacks)
    assert all(value is None or len(value.encode()) <= 64 for value in callbacks)


@pytest.mark.asyncio
async def test_task_card_escapes_user_text_for_telegram_html(session) -> None:
    manager = User(
        telegram_id=715,
        full_name="Администратор <тест>",
        normalized_full_name="администратор <тест>",
        role=Role.ADMIN,
        status=UserStatus.ACTIVE,
    )
    session.add(manager)
    await session.flush()
    task = Task(
        title="Задача <важная>",
        description="Описание с & символом",
        deadline=datetime.now(UTC) + timedelta(days=1),
        creator_id=manager.id,
        idempotency_key="task-card-html-escape",
    )
    session.add(task)
    await session.flush()
    session.add(TaskMember(task_id=task.id, user_id=manager.id, is_creator=True))
    await session.commit()

    text, _ = await task_card(session, task, manager)

    assert "<b>Задача &lt;важная&gt;</b>" in text
    assert "Описание с &amp; символом" in text
    assert "Администратор &lt;тест&gt;" in text
