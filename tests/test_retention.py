from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from apps.api.app.models import (
    Event,
    Notification,
    Role,
    Task,
    TaskPhoto,
    TaskReport,
    User,
    UserStatus,
)
from apps.worker.app import tasks as worker_tasks


class ExistingSession:
    def __init__(self, session) -> None:
        self.session = session

    async def __aenter__(self):
        return self.session

    async def __aexit__(self, exc_type, exc, traceback):
        return False


async def create_expired_archive(session):
    admin = User(
        telegram_id=951,
        full_name="Администратор Архива",
        normalized_full_name="администратор архива",
        status=UserStatus.ACTIVE,
        role=Role.ADMIN,
    )
    session.add(admin)
    await session.flush()
    event = Event(
        title="Архив для удаления",
        starts_at=datetime.now(UTC),
        created_by_id=admin.id,
        retention_delete_at=datetime.now(UTC) - timedelta(minutes=1),
    )
    session.add(event)
    await session.flush()
    task = Task(
        event_id=event.id,
        title="Задача архива",
        deadline=datetime.now(UTC),
        creator_id=admin.id,
        idempotency_key="retention-worker-test",
    )
    session.add(task)
    await session.flush()
    report = TaskReport(task_id=task.id, submitted_by_id=admin.id, comment="Удалить отчёт")
    session.add(report)
    await session.flush()
    photo = TaskPhoto(
        report_id=report.id,
        object_key="tasks/archive/photo.jpg",
        preview_object_key="previews/archive/photo.jpg",
        content_type="image/jpeg",
        size_bytes=42,
        uploaded_by_id=admin.id,
        width=10,
        height=10,
    )
    session.add(photo)
    await session.commit()
    return admin, event, report, photo


@pytest.mark.asyncio
async def test_retention_warns_thirty_days_before_effective_extended_limit(
    session, monkeypatch
) -> None:
    admin = User(
        telegram_id=950,
        full_name="Администратор Хранения",
        normalized_full_name="администратор хранения",
        status=UserStatus.ACTIVE,
        role=Role.ADMIN,
    )
    session.add(admin)
    await session.flush()
    event = Event(
        title="Продлённый архив",
        starts_at=datetime.now(UTC),
        created_by_id=admin.id,
        retention_delete_at=datetime.now(UTC) + timedelta(days=1),
        retention_extended_until=datetime.now(UTC) + timedelta(days=29),
    )
    session.add(event)
    await session.commit()
    monkeypatch.setattr(worker_tasks, "SessionLocal", lambda: ExistingSession(session))

    await worker_tasks._process_archive_retention()
    await session.refresh(event)
    notices = list(
        await session.scalars(
            select(Notification).where(
                Notification.event_id == event.id,
                Notification.type == "ARCHIVE_DELETION_30D",
            )
        )
    )

    assert event.purged_at is None
    assert event.retention_warning_sent_at is not None
    assert len(notices) == 1
    assert notices[0].user_id == admin.id


@pytest.mark.asyncio
async def test_retention_purge_deletes_objects_and_photo_rows_only_after_success(
    session, monkeypatch
) -> None:
    _, event, report, photo = await create_expired_archive(session)
    deleted: list[str] = []

    def delete_success(key: str) -> None:
        deleted.append(key)

    monkeypatch.setattr(worker_tasks, "SessionLocal", lambda: ExistingSession(session))
    monkeypatch.setattr(worker_tasks, "delete_object", delete_success)

    await worker_tasks._process_archive_retention()
    await session.refresh(event)
    await session.refresh(report)

    assert event.purged_at is not None
    assert report.comment is None
    assert report.approval_comment is None
    assert deleted == [photo.object_key, photo.preview_object_key]
    assert await session.get(TaskPhoto, photo.id) is None


@pytest.mark.asyncio
async def test_retention_does_not_mark_archive_purged_when_object_delete_fails(
    session, monkeypatch
) -> None:
    _, event, report, photo = await create_expired_archive(session)

    def delete_failure(_: str) -> None:
        raise RuntimeError("storage unavailable")

    monkeypatch.setattr(worker_tasks, "SessionLocal", lambda: ExistingSession(session))
    monkeypatch.setattr(worker_tasks, "delete_object", delete_failure)

    await worker_tasks._process_archive_retention()
    await session.refresh(event)
    await session.refresh(report)

    assert event.purged_at is None
    assert event.retention_warning_sent_at is not None
    assert report.comment == "Удалить отчёт"
    assert await session.get(TaskPhoto, photo.id) is not None
