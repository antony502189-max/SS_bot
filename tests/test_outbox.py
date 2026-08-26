from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from apps.api.app.models import (
    ChatStatus,
    MembershipState,
    Notification,
    OutboxEvent,
    Role,
    Task,
    TaskChat,
    TaskChatMember,
    TaskMember,
    User,
    UserStatus,
)
from apps.telegram_user_service.app.client import TelegramResult, TelegramResultKind
from apps.worker.app import tasks as worker_tasks
from apps.worker.app.tasks import RetryableOutboxError, raise_if_retryable


def test_flood_wait_preserves_telegram_retry_duration() -> None:
    with pytest.raises(RetryableOutboxError) as error:
        raise_if_retryable(
            TelegramResult(
                TelegramResultKind.FLOOD_WAIT, retry_after_seconds=137, error="FloodWait"
            )
        )
    assert error.value.retry_after_seconds == 137


def test_permanent_telegram_error_is_not_scheduled_as_a_retry() -> None:
    assert raise_if_retryable(TelegramResult(TelegramResultKind.PERMANENT_ERROR)) is None


@pytest.mark.asyncio
async def test_outbox_uses_flood_wait_duration_for_rescheduling(session, monkeypatch) -> None:
    event = OutboxEvent(
        event_type="TASK_CREATED",
        aggregate_type="task",
        aggregate_id="test-task",
        payload={"task_id": "test-task"},
        available_at=datetime.now(UTC) - timedelta(seconds=1),
    )
    session.add(event)
    await session.commit()

    class ExistingSession:
        async def __aenter__(self):
            return session

        async def __aexit__(self, exc_type, exc, traceback):
            return False

    async def flood_wait(_: str) -> None:
        raise RetryableOutboxError(
            TelegramResult(TelegramResultKind.FLOOD_WAIT, retry_after_seconds=137)
        )

    monkeypatch.setattr(worker_tasks, "SessionLocal", lambda: ExistingSession())
    monkeypatch.setattr(worker_tasks, "provision_task_chat", flood_wait)
    before = datetime.now(UTC)
    await worker_tasks._process_outbox()
    await session.refresh(event)
    after = datetime.now(UTC)
    available_at = event.available_at
    if available_at.tzinfo is None:
        available_at = available_at.replace(tzinfo=UTC)

    assert event.processed_at is None
    assert event.attempts == 1
    assert before + timedelta(seconds=136) <= available_at <= after + timedelta(seconds=138)


@pytest.mark.asyncio
async def test_invite_reminder_checks_membership_before_sending(session, monkeypatch) -> None:
    user = User(
        telegram_id=901,
        full_name="Участник Напоминания",
        normalized_full_name="участник напоминания",
        status=UserStatus.ACTIVE,
        role=Role.ADMIN,
    )
    session.add(user)
    await session.flush()
    task = Task(
        title="Проверить вступление",
        deadline=datetime.now(UTC) + timedelta(days=1),
        creator_id=user.id,
        idempotency_key="reminder-membership-check",
    )
    session.add(task)
    await session.flush()
    session.add(TaskMember(task_id=task.id, user_id=user.id, is_creator=True))
    chat = TaskChat(task_id=task.id, telegram_chat_id=-1001234567890, status=ChatStatus.READY)
    session.add(chat)
    await session.flush()
    member = TaskChatMember(
        task_chat_id=chat.id,
        user_id=user.id,
        state=MembershipState.INVITED,
        invite_link="https://t.me/+private-link",
        next_reminder_at=datetime.now(UTC) - timedelta(minutes=1),
    )
    session.add(member)
    await session.commit()

    class ExistingSession:
        async def __aenter__(self):
            return session

        async def __aexit__(self, exc_type, exc, traceback):
            return False

    class JoinedTelegramService:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, traceback):
            return False

        async def is_user_in_chat(self, chat_id: int, username: str | None) -> TelegramResult:
            assert chat_id == -1001234567890
            assert username is None
            return TelegramResult(TelegramResultKind.SUCCESS)

    sent: list[int] = []

    async def unexpected_notification(telegram_id: int, _: str) -> None:
        sent.append(telegram_id)

    monkeypatch.setattr(worker_tasks, "SessionLocal", lambda: ExistingSession())
    monkeypatch.setattr(worker_tasks, "TelegramUserService", JoinedTelegramService)
    monkeypatch.setattr(worker_tasks, "notify", unexpected_notification)

    await worker_tasks._send_invite_reminders()
    await session.refresh(member)

    assert member.state == MembershipState.JOINED
    assert member.next_reminder_at is None
    assert member.reminder_count == 0
    assert sent == []


@pytest.mark.asyncio
async def test_deadline_worker_marks_task_overdue_and_queues_notification(
    session, monkeypatch
) -> None:
    user = User(
        telegram_id=902,
        full_name="Просроченный Участник",
        normalized_full_name="просроченный участник",
        status=UserStatus.ACTIVE,
        role=Role.ADMIN,
    )
    session.add(user)
    await session.flush()
    task = Task(
        title="Срочная задача",
        deadline=datetime.now(UTC) - timedelta(minutes=1),
        creator_id=user.id,
        idempotency_key="deadline-worker",
    )
    session.add(task)
    await session.flush()
    session.add(TaskMember(task_id=task.id, user_id=user.id, is_creator=True))
    await session.commit()

    class ExistingSession:
        async def __aenter__(self):
            return session

        async def __aexit__(self, exc_type, exc, traceback):
            return False

    monkeypatch.setattr(worker_tasks, "SessionLocal", lambda: ExistingSession())

    await worker_tasks._process_task_deadlines()
    await session.refresh(task)
    notifications = list(
        await session.scalars(
            select(Notification).where(
                Notification.task_id == task.id, Notification.type == "TASK_OVERDUE"
            )
        )
    )

    assert task.status.value == "overdue"
    assert len(notifications) == 1
