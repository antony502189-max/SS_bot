from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from apps.api.app.models import Event, Role, Task, TaskMember, User, UserStatus
from apps.api.app.routers.events import archive_out
from apps.api.app.schemas import EventCreate


def test_event_end_must_not_precede_start() -> None:
    start = datetime(2026, 9, 1, tzinfo=UTC)
    with pytest.raises(ValidationError, match="end"):
        EventCreate(title="Событие", starts_at=start, ends_at=start - timedelta(minutes=1))


@pytest.mark.asyncio
async def test_event_archive_includes_users_assigned_only_through_tasks(session) -> None:
    creator = User(
        telegram_id=901,
        full_name="Администратор Архива",
        normalized_full_name="администратор архива",
        status=UserStatus.ACTIVE,
        role=Role.ADMIN,
    )
    worker = User(
        telegram_id=902,
        full_name="Исполнитель Архива",
        normalized_full_name="исполнитель архива",
        status=UserStatus.ACTIVE,
    )
    session.add_all([creator, worker])
    await session.flush()
    event = Event(title="Событие", starts_at=datetime.now(UTC), created_by_id=creator.id)
    session.add(event)
    await session.flush()
    task = Task(
        event_id=event.id,
        title="Задача события",
        deadline=datetime.now(UTC) + timedelta(days=1),
        creator_id=creator.id,
        idempotency_key="archive-task-member",
    )
    session.add(task)
    await session.flush()
    session.add(TaskMember(task_id=task.id, user_id=worker.id))
    await session.commit()

    archive = await archive_out(session, event)

    assert [participant.id for participant in archive.participants] == [worker.id]
