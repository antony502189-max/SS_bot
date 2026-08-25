import asyncio
import uuid
from datetime import UTC, datetime, timedelta

from aiogram import Bot
from celery import Celery
from sqlalchemy import select

from apps.api.app.config import get_settings
from apps.api.app.db import SessionLocal
from apps.api.app.models import (
    ChatStatus,
    MembershipState,
    Notification,
    OutboxEvent,
    Task,
    TaskChat,
    TaskChatMember,
    TaskKind,
    TaskMember,
    TaskStatus,
    User,
)
from apps.api.app.services import queue_task_notifications
from apps.telegram_user_service.app.client import TelegramResultKind, TelegramUserService

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
}


async def notify(telegram_id: int, text: str) -> None:
    if not settings.telegram_bot_token:
        return
    bot = Bot(settings.telegram_bot_token)
    try:
        await bot.send_message(telegram_id, text)
    finally:
        await bot.session.close()


def notification_text(notification: Notification) -> str:
    title = str(notification.payload.get("title", "task"))
    if notification.type == "TASK_ASSIGNED":
        return f"You were assigned to: {title}"
    if notification.type == "TASK_UPDATED":
        return f"Task updated: {title}"
    if notification.type == "TASK_DEADLINE_24H":
        return f"Deadline reminder: {title} is due within 24 hours."
    if notification.type == "TASK_OVERDUE":
        return f"Overdue task: {title} has passed its deadline."
    if notification.type == "TASK_CANCELLED":
        return f"Task cancelled: {title}"
    if notification.type == "TASK_SUBMITTED":
        return f"Report submitted for: {title}"
    if notification.type == "TASK_COMPLETED":
        return f"Task completed: {title}"
    return f"SS Bot notification: {title}"


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
        if not chat:
            chat = TaskChat(task_id=task.id, status=ChatStatus.PENDING)
            session.add(chat)
            await session.flush()
        try:
            async with TelegramUserService() as telegram:
                created = await telegram.create_supergroup(
                    f"Task: {task.title}", task.description or "Temporary SS Bot working group"
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
                    chat.status = ChatStatus.FAILED
                    chat.last_error = f"bot_add:{bot_result.error or bot_result.kind.value}"
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
                    user = await session.get(User, member.user_id)
                    entry = TaskChatMember(task_chat_id=chat.id, user_id=member.user_id)
                    session.add(entry)
                    direct = await telegram.invite_user(
                        chat.telegram_chat_id, user.telegram_username if user else None
                    )
                    if direct.kind == TelegramResultKind.SUCCESS:
                        entry.state = MembershipState.JOINED
                    elif direct.kind in {
                        TelegramResultKind.PRIVACY_RESTRICTED,
                        TelegramResultKind.USERNAME_NOT_FOUND,
                    }:
                        invite = await telegram.create_single_use_invite(
                            chat.telegram_chat_id, f"task-{task.id}"
                        )
                        if invite.kind == TelegramResultKind.SUCCESS:
                            entry.state = MembershipState.INVITED
                            entry.invite_link = str(invite.value)
                            entry.invite_link_created_at = datetime.now(UTC)
                            entry.next_reminder_at = datetime.now(UTC) + timedelta(minutes=30)
                            if user:
                                await notify(
                                    user.telegram_id,
                                    f"Join the working group for ‘{task.title}’: {invite.value}",
                                )
                        else:
                            entry.state = MembershipState.FAILED
                            entry.last_error = invite.error or invite.kind.value
                    else:
                        entry.state = MembershipState.FAILED
                        entry.last_error = direct.error or direct.kind.value
                chat.status = ChatStatus.READY
        except Exception as exc:
            chat.status = ChatStatus.FAILED
            chat.last_error = type(exc).__name__
            raise
        finally:
            await session.commit()


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
                event.processed_at = datetime.now(UTC)
                event.last_error = None
            except Exception as exc:
                event.last_error = type(exc).__name__
        await session.commit()


@celery_app.task(name="apps.worker.app.tasks.process_outbox")
def process_outbox() -> None:
    asyncio.run(_process_outbox())


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
                    f"Reminder: join your task working group: {member.invite_link}",
                )
            member.next_reminder_at = now + timedelta(minutes=30)
        await session.commit()


@celery_app.task(name="apps.worker.app.tasks.send_invite_reminders")
def send_invite_reminders() -> None:
    asyncio.run(_send_invite_reminders())


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
                            f"The working group for ‘{task.title}’ will be deleted in 24 hours.",
                        )
                chat.cleanup_warned_at = now
            if task.cleanup_at <= now and chat.telegram_chat_id:
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


@celery_app.task(name="apps.worker.app.tasks.process_cleanup")
def process_cleanup() -> None:
    asyncio.run(_process_cleanup())
