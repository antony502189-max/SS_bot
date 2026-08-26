import asyncio
import uuid
from datetime import UTC, datetime, timedelta

from celery import Celery
from sqlalchemy import select

from apps.api.app.config import get_settings
from apps.api.app.db import SessionLocal
from apps.api.app.models import (
    ChatStatus,
    Event,
    MembershipState,
    Notification,
    OutboxEvent,
    Role,
    Task,
    TaskChat,
    TaskChatMember,
    TaskKind,
    TaskMember,
    TaskPhoto,
    TaskReport,
    TaskStatus,
    User,
    UserStatus,
)
from apps.api.app.services import queue_task_notifications, refresh_event_retention
from apps.api.app.storage import delete_object, object_exists
from apps.api.app.telegram_bot import build_telegram_bot
from apps.telegram_user_service.app.client import (
    TelegramResult,
    TelegramResultKind,
    TelegramUserService,
)

settings = get_settings()
celery_app = Celery("ss_bot", broker=settings.redis_url, backend=settings.redis_url)
celery_app.conf.beat_schedule = {
    "outbox-every-minute": {"task": "apps.worker.app.tasks.process_outbox", "schedule": 60.0},
    "invite-reminders-every-minute": {
        "task": "apps.worker.app.tasks.send_invite_reminders",
        "schedule": 60.0,
    },
    "cleanup-every-hour": {"task": "apps.worker.app.tasks.process_cleanup", "schedule": 3600.0},
    "notifications-every-minute": {
        "task": "apps.worker.app.tasks.send_due_notifications",
        "schedule": 60.0,
    },
    "task-deadlines-every-five-minutes": {
        "task": "apps.worker.app.tasks.process_task_deadlines",
        "schedule": 300.0,
    },
    "reconcile-task-chat-members-every-ten-minutes": {
        "task": "apps.worker.app.tasks.reconcile_task_chat_members",
        "schedule": 600.0,
    },
    "process-archive-retention-daily": {
        "task": "apps.worker.app.tasks.process_archive_retention",
        "schedule": 86400.0,
    },
}


async def notify(telegram_id: int, text: str) -> None:
    if not settings.telegram_bot_token:
        return
    bot = build_telegram_bot(settings.telegram_bot_token)
    try:
        await bot.send_message(telegram_id, text)
    finally:
        await bot.session.close()


def notification_text(notification: Notification) -> str:
    title = str(notification.payload.get("title", "задача"))
    if notification.type == "TASK_ASSIGNED":
        return f"Вам назначена задача: {title}"
    if notification.type == "TASK_UPDATED":
        return f"Задача обновлена: {title}"
    if notification.type == "TASK_DEADLINE_24H":
        return f"Напоминание: срок задачи «{title}» наступит в течение 24 часов."
    if notification.type == "TASK_OVERDUE":
        return f"Задача просрочена: «{title}»."
    if notification.type == "TASK_CANCELLED":
        return f"Задача отменена: {title}"
    if notification.type == "TASK_SUBMITTED":
        return f"По задаче отправлен отчёт: {title}"
    if notification.type == "TASK_COMPLETED":
        return f"Задача выполнена: {title}"
    if notification.type == "ARCHIVE_DELETION_30D":
        return f"Внимание: архив «{title}» будет удалён через 30 дней."
    return f"Уведомление SS Bot: {title}"


async def _send_due_notifications() -> None:
    if not settings.telegram_bot_token:
        return
    now = datetime.now(UTC)
    async with SessionLocal() as session:
        due = list(
            (
                await session.scalars(
                    select(Notification)
                    .where(Notification.status == "pending", Notification.scheduled_at <= now)
                    .with_for_update(skip_locked=True)
                    .limit(100)
                )
            ).all()
        )
        for notification in due:
            notification.attempts += 1
            user = await session.get(User, notification.user_id)
            try:
                if not user:
                    notification.status = "cancelled"
                    notification.last_error = "user_not_found"
                    continue
                await notify(user.telegram_id, notification_text(notification))
                notification.status = "sent"
                notification.sent_at = now
                notification.last_error = None
            except Exception as exc:
                notification.last_error = type(exc).__name__
                notification.next_attempt_at = now + timedelta(
                    minutes=min(30, 2**notification.attempts)
                )
                notification.scheduled_at = notification.next_attempt_at
                if notification.attempts >= 5:
                    notification.status = "failed"
                    notification.failed_at = now
        await session.commit()


@celery_app.task(name="apps.worker.app.tasks.send_due_notifications")
def send_due_notifications() -> None:
    asyncio.run(_send_due_notifications())


async def _process_task_deadlines() -> None:
    now = datetime.now(UTC)
    reminder_cutoff = now + timedelta(hours=settings.task_deadline_reminder_hours)
    async with SessionLocal() as session:
        overdue = list(
            (
                await session.scalars(
                    select(Task)
                    .where(Task.status == TaskStatus.ACTIVE, Task.deadline < now)
                    .with_for_update(skip_locked=True)
                )
            ).all()
        )
        for task in overdue:
            task.status = TaskStatus.OVERDUE
            await queue_task_notifications(session, task, {task.creator_id}, "TASK_OVERDUE")
        due_soon = list(
            (
                await session.scalars(
                    select(Task)
                    .where(
                        Task.status == TaskStatus.ACTIVE,
                        Task.deadline >= now,
                        Task.deadline <= reminder_cutoff,
                    )
                    .with_for_update(skip_locked=True)
                )
            ).all()
        )
        for task in due_soon:
            already_queued = await session.scalar(
                select(Notification.id).where(
                    Notification.task_id == task.id,
                    Notification.type == "TASK_DEADLINE_24H",
                )
            )
            if already_queued:
                continue
            members = {
                member.user_id
                for member in (
                    await session.scalars(select(TaskMember).where(TaskMember.task_id == task.id))
                ).all()
            }
            await queue_task_notifications(session, task, members, "TASK_DEADLINE_24H")
        await session.commit()


@celery_app.task(name="apps.worker.app.tasks.process_task_deadlines")
def process_task_deadlines() -> None:
    asyncio.run(_process_task_deadlines())


async def provision_task_chat(task_id: str) -> None:
    async with SessionLocal() as session:
        task = await session.get(Task, uuid.UUID(task_id))
        if not task or task.kind != TaskKind.GROUP:
            return
        chat = await session.scalar(select(TaskChat).where(TaskChat.task_id == task.id))
        if chat and chat.status == ChatStatus.READY:
            return
        if chat and chat.telegram_chat_id:
            chat.status = ChatStatus.DEGRADED
            chat.last_error = "existing_group_requires_recovery"
            await session.commit()
            return
        if not chat:
            chat = TaskChat(task_id=task.id, status=ChatStatus.PENDING)
            session.add(chat)
            await session.flush()
        chat.status = ChatStatus.CREATING
        try:
            async with TelegramUserService() as telegram:
                created = await telegram.create_supergroup(
                    f"Задача: {task.title}", task.description or "Рабочая группа SS Bot"
                )
                if created.kind != TelegramResultKind.SUCCESS:
                    chat.status = ChatStatus.FAILED
                    chat.last_error = created.error or created.kind.value
                    await session.commit()
                    return
                chat.telegram_chat_id = int(created.value)
                bot_result = await telegram.add_bot(
                    chat.telegram_chat_id, settings.telegram_bot_username
                )
                if bot_result.kind != TelegramResultKind.SUCCESS:
                    chat.status = ChatStatus.DEGRADED
                    chat.last_error = f"bot_add:{bot_result.error or bot_result.kind.value}"
                    await session.commit()
                    return
                brief = await ensure_task_brief(telegram, task, chat)
                if brief.kind != TelegramResultKind.SUCCESS:
                    chat.status = ChatStatus.DEGRADED
                    chat.last_error = f"task_brief:{brief.error or brief.kind.value}"
                    await session.commit()
                    return
                members = list(
                    (
                        await session.scalars(
                            select(TaskMember).where(TaskMember.task_id == task.id)
                        )
                    ).all()
                )
                for member in members:
                    await invite_task_chat_member(session, telegram, task, chat, member.user_id)
                chat.status = ChatStatus.READY
        except Exception as exc:
            chat.status = ChatStatus.DEGRADED if chat.telegram_chat_id else ChatStatus.FAILED
            chat.last_error = type(exc).__name__
            raise
        finally:
            await session.commit()


async def invite_task_chat_member(
    session, telegram: TelegramUserService, task: Task, chat: TaskChat, user_id: uuid.UUID
) -> None:
    """Invite one assigned user, preserving an auditable delivery state for retries."""
    if not chat.telegram_chat_id:
        raise RuntimeError("Working group is missing its Telegram ID")
    user = await session.get(User, user_id)
    entry = await session.scalar(
        select(TaskChatMember).where(
            TaskChatMember.task_chat_id == chat.id, TaskChatMember.user_id == user_id
        )
    )
    if not entry:
        entry = TaskChatMember(task_chat_id=chat.id, user_id=user_id)
        session.add(entry)
        await session.flush()
    if entry.invite_link:
        await telegram.revoke_invite(chat.telegram_chat_id, entry.invite_link)
    entry.invite_link = None
    entry.invite_link_created_at = None
    entry.next_reminder_at = None
    entry.last_error = None
    direct = await telegram.invite_user(
        chat.telegram_chat_id, user.telegram_username if user else None
    )
    now = datetime.now(UTC)
    if direct.kind == TelegramResultKind.SUCCESS:
        entry.state = MembershipState.JOINED
        entry.joined_at = entry.joined_at or now
        entry.last_checked_at = now
        return
    if direct.kind not in {
        TelegramResultKind.PRIVACY_RESTRICTED,
        TelegramResultKind.USERNAME_NOT_FOUND,
    }:
        entry.state = MembershipState.FAILED
        entry.last_error = direct.error or direct.kind.value
        return
    invite = await telegram.create_single_use_invite(chat.telegram_chat_id, f"task-{task.id}")
    if invite.kind != TelegramResultKind.SUCCESS:
        entry.state = MembershipState.FAILED
        entry.last_error = invite.error or invite.kind.value
        return
    entry.state = MembershipState.INVITED
    entry.invite_link = str(invite.value)
    entry.invite_link_created_at = now
    entry.next_reminder_at = now + timedelta(minutes=30)
    if user:
        await notify(user.telegram_id, f"Вступите в рабочую группу «{task.title}»: {invite.value}")


async def _process_outbox() -> None:
    async with SessionLocal() as session:
        events = list(
            (
                await session.scalars(
                    select(OutboxEvent)
                    .where(OutboxEvent.processed_at.is_(None))
                    .order_by(OutboxEvent.created_at)
                    .with_for_update(skip_locked=True)
                    .limit(20)
                )
            ).all()
        )
        for event in events:
            event.attempts += 1
            try:
                if event.event_type == "TASK_CREATED":
                    await provision_task_chat(event.payload["task_id"])
                elif event.event_type == "TASK_CHAT_MEMBER_INVITE_REQUESTED":
                    await _invite_requested_task_chat_member(
                        event.payload["task_id"], event.payload["user_id"]
                    )
                elif event.event_type == "TASK_CHAT_RECOVERY_REQUESTED":
                    await _recover_task_chat(event.payload["task_id"])
                elif event.event_type == "TASK_CHAT_MEMBER_REMOVAL_REQUESTED":
                    await _remove_task_chat_member(
                        event.payload["task_id"], event.payload["user_id"]
                    )
                event.processed_at = datetime.now(UTC)
                event.last_error = None
            except Exception as exc:
                event.last_error = type(exc).__name__
        await session.commit()


@celery_app.task(name="apps.worker.app.tasks.process_outbox")
def process_outbox() -> None:
    asyncio.run(_process_outbox())


async def _invite_requested_task_chat_member(task_id: str, user_id: str) -> None:
    async with SessionLocal() as session:
        task = await session.get(Task, uuid.UUID(task_id))
        if not task or task.kind != TaskKind.GROUP:
            return
        chat = await session.scalar(select(TaskChat).where(TaskChat.task_id == task.id))
        if not chat or chat.status != ChatStatus.READY or not chat.telegram_chat_id:
            raise RuntimeError("Working group is not ready for member invitation")
        if not await session.scalar(
            select(TaskMember).where(
                TaskMember.task_id == task.id, TaskMember.user_id == uuid.UUID(user_id)
            )
        ):
            return
        async with TelegramUserService() as telegram:
            await invite_task_chat_member(session, telegram, task, chat, uuid.UUID(user_id))
        await session.commit()


async def _recover_task_chat(task_id: str) -> None:
    async with SessionLocal() as session:
        task = await session.get(Task, uuid.UUID(task_id))
        if not task or task.kind != TaskKind.GROUP:
            return
        chat = await session.scalar(select(TaskChat).where(TaskChat.task_id == task.id))
        if not chat or not chat.telegram_chat_id:
            raise RuntimeError("No existing Telegram working group to recover")
        try:
            async with TelegramUserService() as telegram:
                bot_result = await telegram.add_bot(
                    chat.telegram_chat_id, settings.telegram_bot_username
                )
                if bot_result.kind != TelegramResultKind.SUCCESS:
                    chat.status = ChatStatus.DEGRADED
                    chat.last_error = f"bot_add:{bot_result.error or bot_result.kind.value}"
                    await session.commit()
                    return
                brief = await ensure_task_brief(telegram, task, chat)
                if brief.kind != TelegramResultKind.SUCCESS:
                    chat.status = ChatStatus.DEGRADED
                    chat.last_error = f"task_brief:{brief.error or brief.kind.value}"
                    await session.commit()
                    return
                members = list(
                    (
                        await session.scalars(
                            select(TaskMember).where(TaskMember.task_id == task.id)
                        )
                    ).all()
                )
                for member in members:
                    existing = await session.scalar(
                        select(TaskChatMember).where(
                            TaskChatMember.task_chat_id == chat.id,
                            TaskChatMember.user_id == member.user_id,
                        )
                    )
                    if not existing or existing.state != MembershipState.JOINED:
                        await invite_task_chat_member(session, telegram, task, chat, member.user_id)
                chat.status = ChatStatus.READY
                chat.last_error = None
        except Exception as exc:
            chat.status = ChatStatus.DEGRADED
            chat.last_error = type(exc).__name__
            raise
        finally:
            await session.commit()


async def ensure_task_brief(telegram: TelegramUserService, task: Task, chat: TaskChat):
    if chat.pinned_message_id:
        return TelegramResult(TelegramResultKind.SUCCESS)
    result = await telegram.post_and_pin_task_brief(
        chat.telegram_chat_id, task.title, task.description, task.deadline
    )
    if result.kind == TelegramResultKind.SUCCESS:
        chat.pinned_message_id = int(result.value)
    return result


async def _remove_task_chat_member(task_id: str, user_id: str) -> None:
    async with SessionLocal() as session:
        task = await session.get(Task, uuid.UUID(task_id))
        if not task:
            return
        chat = await session.scalar(select(TaskChat).where(TaskChat.task_id == task.id))
        member = (
            await session.scalar(
                select(TaskChatMember).where(
                    TaskChatMember.task_chat_id == chat.id,
                    TaskChatMember.user_id == uuid.UUID(user_id),
                )
            )
            if chat
            else None
        )
        user = await session.get(User, uuid.UUID(user_id))
        if not chat or not chat.telegram_chat_id or not member or not user:
            return
        async with TelegramUserService() as telegram:
            result = await telegram.remove_user(chat.telegram_chat_id, user.telegram_username)
        if result.kind == TelegramResultKind.SUCCESS:
            member.state = MembershipState.REMOVED
            member.next_reminder_at = None
            member.last_error = None
        else:
            member.last_error = result.error or result.kind.value
            raise RuntimeError(member.last_error)
        await session.commit()


async def _send_invite_reminders() -> None:
    now = datetime.now(UTC)
    async with SessionLocal() as session:
        due = list(
            (
                await session.scalars(
                    select(TaskChatMember)
                    .where(
                        TaskChatMember.state == MembershipState.INVITED,
                        TaskChatMember.next_reminder_at <= now,
                    )
                    .with_for_update(skip_locked=True)
                    .limit(100)
                )
            ).all()
        )
        for member in due:
            user = await session.get(User, member.user_id)
            if user and member.invite_link:
                await notify(
                    user.telegram_id,
                    f"Напоминание: вступите в рабочую группу задачи: {member.invite_link}",
                )
            member.last_reminder_at = now
            member.reminder_count += 1
            member.next_reminder_at = now + timedelta(minutes=30)
        await session.commit()


@celery_app.task(name="apps.worker.app.tasks.send_invite_reminders")
def send_invite_reminders() -> None:
    asyncio.run(_send_invite_reminders())


async def _reconcile_task_chat_members() -> None:
    """Confirm invite-link joins and spot users who left an otherwise active task chat."""
    now = datetime.now(UTC)
    async with SessionLocal() as session:
        rows = list(
            (
                await session.execute(
                    select(TaskChatMember, TaskChat, User)
                    .join(TaskChat, TaskChatMember.task_chat_id == TaskChat.id)
                    .join(User, TaskChatMember.user_id == User.id)
                    .where(
                        TaskChat.status == ChatStatus.READY,
                        TaskChat.telegram_chat_id.is_not(None),
                        TaskChatMember.state.in_([MembershipState.INVITED, MembershipState.JOINED]),
                    )
                    .with_for_update(skip_locked=True)
                    .limit(100)
                )
            ).all()
        )
        if not rows:
            return
        async with TelegramUserService() as telegram:
            for member, chat, user in rows:
                result = await telegram.is_user_in_chat(
                    chat.telegram_chat_id, user.telegram_username
                )
                member.last_checked_at = now
                if result.kind == TelegramResultKind.SUCCESS:
                    member.state = MembershipState.JOINED
                    member.joined_at = member.joined_at or now
                    member.next_reminder_at = None
                    member.last_error = None
                elif result.kind == TelegramResultKind.NOT_JOINED:
                    if member.state == MembershipState.JOINED:
                        member.state = MembershipState.NOT_JOINED
                    member.last_error = None
                elif result.kind == TelegramResultKind.USERNAME_NOT_FOUND:
                    member.last_error = "username_unavailable_for_membership_check"
                else:
                    member.last_error = result.error or result.kind.value
        await session.commit()


@celery_app.task(name="apps.worker.app.tasks.reconcile_task_chat_members")
def reconcile_task_chat_members() -> None:
    asyncio.run(_reconcile_task_chat_members())


def event_retention_limit(event: Event) -> datetime | None:
    return max(
        (value for value in [event.retention_delete_at, event.retention_extended_until] if value),
        default=None,
    )


async def _process_archive_retention() -> None:
    now = datetime.now(UTC)
    warning_cutoff = now + timedelta(days=settings.archive_delete_warning_days)
    async with SessionLocal() as session:
        events = list(
            (
                await session.scalars(
                    select(Event)
                    .where(
                        Event.retention_delete_at.is_not(None),
                        Event.purged_at.is_(None),
                    )
                    .with_for_update(skip_locked=True)
                )
            ).all()
        )
        for event in events:
            deadline = event_retention_limit(event)
            if not deadline:
                continue
            if deadline <= warning_cutoff and not event.retention_warning_sent_at:
                recipients = list(
                    (
                        await session.scalars(
                            select(User).where(
                                User.status == UserStatus.ACTIVE,
                                (User.role == Role.ADMIN)
                                | (
                                    (User.role == Role.SECTOR_HEAD)
                                    & (User.sector_id == event.sector_id)
                                ),
                            )
                        )
                    ).all()
                )
                for user in recipients:
                    session.add(
                        Notification(
                            user_id=user.id,
                            event_id=event.id,
                            type="ARCHIVE_DELETION_30D",
                            payload={"title": event.title},
                        )
                    )
                event.retention_warning_sent_at = now
            if deadline > now:
                continue
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
            try:
                for photo in photos:
                    delete_object(photo.object_key)
                    if photo.preview_object_key:
                        delete_object(photo.preview_object_key)
            except Exception:
                # Do not mark the archive purged until every object deletion succeeded.
                event.retention_warning_sent_at = now
                continue
            reports = list(
                (
                    await session.scalars(
                        select(TaskReport).join(Task).where(Task.event_id == event.id)
                    )
                ).all()
            )
            for report in reports:
                report.comment = None
                report.approval_comment = None
            event.purged_at = now
        await session.commit()


@celery_app.task(name="apps.worker.app.tasks.process_archive_retention")
def process_archive_retention() -> None:
    asyncio.run(_process_archive_retention())


async def _process_cleanup() -> None:
    now = datetime.now(UTC)
    async with SessionLocal() as session:
        chats = list(
            (
                await session.scalars(
                    select(TaskChat)
                    .join(Task)
                    .where(
                        Task.cleanup_at.is_not(None),
                        TaskChat.status.in_([ChatStatus.READY, ChatStatus.CLEANUP_PENDING]),
                    )
                )
            ).all()
        )
        for chat in chats:
            task = await session.get(Task, chat.task_id)
            assert task
            if not chat.cleanup_warned_at and task.cleanup_at - now <= timedelta(hours=24):
                for member in (
                    await session.scalars(
                        select(TaskChatMember).where(TaskChatMember.task_chat_id == chat.id)
                    )
                ).all():
                    user = await session.get(User, member.user_id)
                    if user:
                        await notify(
                            user.telegram_id,
                            f"Рабочая группа задачи «{task.title}» будет удалена через 24 часа.",
                        )
                chat.cleanup_warned_at = now
            if task.cleanup_at <= now and chat.telegram_chat_id:
                if not await archive_is_persisted(session, task):
                    chat.status = ChatStatus.CLEANUP_PENDING
                    chat.last_error = "archive_verification_failed"
                    continue
                chat.status = ChatStatus.CLEANUP_PENDING
                try:
                    async with TelegramUserService() as telegram:
                        for member in (
                            await session.scalars(
                                select(TaskChatMember).where(
                                    TaskChatMember.task_chat_id == chat.id,
                                    TaskChatMember.invite_link.is_not(None),
                                )
                            )
                        ).all():
                            await telegram.revoke_invite(chat.telegram_chat_id, member.invite_link)
                        deleted = await telegram.delete_supergroup(chat.telegram_chat_id)
                        if deleted.kind == TelegramResultKind.SUCCESS:
                            chat.status = ChatStatus.DELETED
                            chat.last_error = None
                        else:
                            chat.last_error = deleted.error or deleted.kind.value
                except Exception as exc:
                    chat.last_error = type(exc).__name__
        await session.commit()


async def archive_is_persisted(session, task: Task) -> bool:
    """Keep Telegram disposable: do not delete a chat until stored artifacts are verifiable."""
    if task.event_id:
        await refresh_event_retention(session, task.event_id)
    photos = list(
        (
            await session.scalars(
                select(TaskPhoto)
                .join(TaskReport, TaskPhoto.report_id == TaskReport.id)
                .where(TaskReport.task_id == task.id)
            )
        ).all()
    )
    try:
        return all(
            object_exists(photo.object_key)
            and (not photo.preview_object_key or object_exists(photo.preview_object_key))
            for photo in photos
        )
    except Exception:
        return False


@celery_app.task(name="apps.worker.app.tasks.process_cleanup")
def process_cleanup() -> None:
    asyncio.run(_process_cleanup())
