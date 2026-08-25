import re
import unicodedata
import uuid
from datetime import UTC, datetime, timedelta

from fastapi import HTTPException, status
from sqlalchemy import Select, select
from sqlalchemy.ext.asyncio import AsyncSession

from .models import (
    AuditLog,
    Event,
    EventParticipant,
    Notification,
    OutboxEvent,
    Role,
    Task,
    TaskChecklistItem,
    TaskMember,
    User,
    UserStatus,
)
from .schemas import EventCreate, TaskCreate

_SPACE = re.compile(r"\s+")


def normalize_full_name(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).casefold().replace("ё", "е")
    return _SPACE.sub(" ", normalized.strip())


def utc_now() -> datetime:
    return datetime.now(UTC)


def task_cleanup_at(completed_at: datetime, deadline: datetime) -> datetime:
    """Three days after closure, never before the task deadline."""
    return max(completed_at + timedelta(days=3), deadline)


async def audit(
    session: AsyncSession,
    actor_id: uuid.UUID | None,
    action: str,
    entity_type: str,
    entity_id: uuid.UUID | str,
    details: dict | None = None,
) -> None:
    session.add(
        AuditLog(
            actor_id=actor_id,
            action=action,
            entity_type=entity_type,
            entity_id=str(entity_id),
            details=details or {},
        )
    )


async def require_active_user(session: AsyncSession, user_id: uuid.UUID) -> User:
    user = await session.get(User, user_id)
    if not user or user.status != UserStatus.ACTIVE:
        raise HTTPException(status_code=422, detail="An assigned user must be active")
    return user


async def require_role(session: AsyncSession, user_id: uuid.UUID, *roles: Role) -> User:
    user = await require_active_user(session, user_id)
    if user.role not in roles:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Insufficient role")
    return user


def ensure_sector_access(actor: User, sector_id: uuid.UUID | None) -> None:
    if actor.role == Role.ADMIN:
        return
    if actor.role != Role.SECTOR_HEAD or actor.sector_id != sector_id:
        raise HTTPException(status_code=403, detail="You can manage only your own sector")


async def create_event(session: AsyncSession, actor: User, payload: EventCreate) -> Event:
    ensure_sector_access(actor, payload.sector_id)
    event = Event(
        title=payload.title.strip(),
        description=payload.description,
        starts_at=payload.starts_at,
        ends_at=payload.ends_at,
        budget=payload.budget,
        sector_id=payload.sector_id,
        created_by_id=actor.id,
    )
    session.add(event)
    await session.flush()
    for user_id in set(payload.participant_ids):
        participant = await require_active_user(session, user_id)
        if actor.role == Role.SECTOR_HEAD and participant.sector_id != event.sector_id:
            raise HTTPException(status_code=403, detail="You can assign only users in your sector")
        session.add(EventParticipant(event_id=event.id, user_id=user_id))
    await audit(session, actor.id, "event.created", "event", event.id)
    return event


async def create_task(
    session: AsyncSession, actor: User, payload: TaskCreate, idempotency_key: str
) -> tuple[Task, bool]:
    if not idempotency_key:
        raise HTTPException(status_code=400, detail="Idempotency-Key header is required")
    existing = await session.scalar(select(Task).where(Task.idempotency_key == idempotency_key))
    if existing:
        return existing, True
    ensure_sector_access(actor, payload.sector_id)
    if payload.deadline <= utc_now():
        raise HTTPException(status_code=422, detail="Deadline must be in the future")
    member_ids = set(payload.member_ids)
    member_ids.add(actor.id)
    if payload.leader_id:
        member_ids.add(payload.leader_id)
    if payload.kind.value == "group" and not payload.leader_id:
        raise HTTPException(status_code=422, detail="A group task requires a leader")
    if payload.kind.value == "individual" and len(member_ids - {actor.id}) != 1:
        raise HTTPException(
            status_code=422, detail="An individual task requires exactly one assignee"
        )
    for user_id in member_ids:
        member = await require_active_user(session, user_id)
        if actor.role == Role.SECTOR_HEAD and member.sector_id != payload.sector_id:
            raise HTTPException(status_code=403, detail="You can assign only users in your sector")
    task = Task(
        title=payload.title.strip(),
        description=payload.description,
        kind=payload.kind,
        deadline=payload.deadline,
        event_id=payload.event_id,
        sector_id=payload.sector_id,
        creator_id=actor.id,
        leader_id=payload.leader_id,
        idempotency_key=idempotency_key,
    )
    session.add(task)
    await session.flush()
    for user_id in member_ids:
        session.add(
            TaskMember(
                task_id=task.id,
                user_id=user_id,
                is_creator=user_id == actor.id,
                is_leader=user_id == payload.leader_id,
            )
        )
    for position, title in enumerate(payload.checklist):
        session.add(TaskChecklistItem(task_id=task.id, title=title.strip(), position=position))
    session.add(
        OutboxEvent(
            event_type="TASK_CREATED",
            aggregate_type="task",
            aggregate_id=str(task.id),
            payload={"task_id": str(task.id)},
        )
    )
    await queue_task_notifications(session, task, member_ids, "TASK_ASSIGNED")
    await audit(session, actor.id, "task.created", "task", task.id, {"kind": task.kind.value})
    return task, False


async def queue_task_notifications(
    session: AsyncSession, task: Task, user_ids: set[uuid.UUID], notification_type: str
) -> None:
    for user_id in user_ids:
        if user_id == task.creator_id and notification_type == "TASK_ASSIGNED":
            continue
        session.add(
            Notification(
                user_id=user_id,
                task_id=task.id,
                event_id=task.event_id,
                type=notification_type,
                payload={"task_id": str(task.id), "title": task.title},
            )
        )


async def is_task_member(session: AsyncSession, task_id: uuid.UUID, user_id: uuid.UUID) -> bool:
    return bool(
        await session.scalar(
            select(TaskMember.id).where(
                TaskMember.task_id == task_id, TaskMember.user_id == user_id
            )
        )
    )


async def task_query_for_user(user_id: uuid.UUID) -> Select[tuple[Task]]:
    return (
        select(Task).join(TaskMember).where(TaskMember.user_id == user_id).order_by(Task.deadline)
    )
