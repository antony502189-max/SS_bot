import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, Header, HTTPException, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..db import get_session
from ..dependencies import current_user
from ..models import (
    ChatStatus,
    MembershipState,
    OutboxEvent,
    Role,
    Task,
    TaskChat,
    TaskChatMember,
    TaskChecklistItem,
    TaskMember,
    TaskStatus,
    User,
)
from ..schemas import (
    ChecklistItemCreate,
    ChecklistItemOut,
    ChecklistUpdate,
    TaskChatMemberOut,
    TaskChatOut,
    TaskCreate,
    TaskDetail,
    TaskMemberCreate,
    TaskMemberOut,
    TaskOut,
    TaskUpdate,
    UserOut,
)
from ..services import (
    audit,
    create_task,
    ensure_sector_access,
    is_task_member,
    queue_task_notifications,
    refresh_event_retention,
    require_active_user,
    require_role,
    task_cleanup_at,
    task_query_for_user,
)

router = APIRouter(prefix="/tasks", tags=["tasks"])


async def chat_out(session: AsyncSession, chat: TaskChat) -> TaskChatOut:
    rows = (
        await session.execute(
            select(TaskChatMember, User).join(User).where(TaskChatMember.task_chat_id == chat.id)
        )
    ).all()
    return TaskChatOut(
        id=chat.id,
        telegram_chat_id=chat.telegram_chat_id,
        status=chat.status,
        last_error=chat.last_error,
        cleanup_warned_at=chat.cleanup_warned_at,
        members=[
            TaskChatMemberOut(
                user=UserOut.model_validate(user, from_attributes=True),
                state=member.state,
                next_reminder_at=member.next_reminder_at,
                last_reminder_at=member.last_reminder_at,
                reminder_count=member.reminder_count,
                joined_at=member.joined_at,
                last_checked_at=member.last_checked_at,
                last_error=member.last_error,
            )
            for member, user in rows
        ],
    )


async def task_for_manager(session: AsyncSession, task_id: uuid.UUID, actor: User) -> Task:
    task = await session.get(Task, task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    await require_role(session, actor.id, Role.ADMIN, Role.SECTOR_HEAD)
    ensure_sector_access(actor, task.sector_id)
    return task


async def task_detail(session: AsyncSession, task: Task) -> TaskDetail:
    members = list(
        (
            await session.execute(
                select(TaskMember, User).join(User).where(TaskMember.task_id == task.id)
            )
        ).all()
    )
    checklist = list(
        (
            await session.scalars(
                select(TaskChecklistItem)
                .where(TaskChecklistItem.task_id == task.id)
                .order_by(TaskChecklistItem.position)
            )
        ).all()
    )
    return TaskDetail.model_validate(task, from_attributes=True).model_copy(
        update={
            "members": [
                TaskMemberOut(
                    user=UserOut.model_validate(user, from_attributes=True),
                    is_creator=member.is_creator,
                    is_leader=member.is_leader,
                )
                for member, user in members
            ],
            "checklist": [
                ChecklistItemOut.model_validate(item, from_attributes=True) for item in checklist
            ],
        }
    )


@router.get("", response_model=list[TaskOut])
async def my_tasks(
    actor: User = Depends(current_user), session: AsyncSession = Depends(get_session)
) -> list[Task]:
    return list((await session.scalars(await task_query_for_user(actor.id))).unique().all())


@router.post("", response_model=TaskOut, status_code=201)
async def create_task_route(
    body: TaskCreate,
    response: Response,
    actor: User = Depends(current_user),
    idempotency_key: str = Header(alias="Idempotency-Key", default=""),
    session: AsyncSession = Depends(get_session),
) -> Task:
    await require_role(session, actor.id, Role.ADMIN, Role.SECTOR_HEAD)
    task, reused = await create_task(session, actor, body, idempotency_key)
    if reused:
        response.status_code = 200
        return task
    await session.commit()
    await session.refresh(task)
    return task


@router.get("/{task_id}", response_model=TaskDetail)
async def get_task(
    task_id: uuid.UUID,
    actor: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> TaskDetail:
    if not await is_task_member(session, task_id, actor.id):
        raise HTTPException(status_code=403, detail="Task membership required")
    task = await session.get(Task, task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    return await task_detail(session, task)


@router.get("/{task_id}/chat", response_model=TaskChatOut)
async def get_task_chat(
    task_id: uuid.UUID,
    actor: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> TaskChatOut:
    if not await is_task_member(session, task_id, actor.id):
        raise HTTPException(status_code=403, detail="Task membership required")
    chat = await session.scalar(select(TaskChat).where(TaskChat.task_id == task_id))
    if not chat:
        raise HTTPException(status_code=404, detail="This task has no working group")
    return await chat_out(session, chat)


@router.post("/{task_id}/chat/retry", response_model=TaskChatOut)
async def retry_task_chat(
    task_id: uuid.UUID,
    actor: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> TaskChatOut:
    task = await task_for_manager(session, task_id, actor)
    chat = await session.scalar(select(TaskChat).where(TaskChat.task_id == task.id))
    if not chat:
        raise HTTPException(status_code=422, detail="Only group tasks have a working group")
    if chat.status == ChatStatus.READY:
        raise HTTPException(status_code=422, detail="Working group is already active")
    if chat.telegram_chat_id:
        raise HTTPException(status_code=422, detail="Existing group requires member-level recovery")
    chat.status = ChatStatus.PENDING
    chat.last_error = None
    session.add(
        OutboxEvent(
            event_type="TASK_CREATED",
            aggregate_type="task",
            aggregate_id=str(task.id),
            payload={"task_id": str(task.id)},
        )
    )
    await audit(session, actor.id, "task.chat_retry_requested", "task", task.id)
    await session.commit()
    await session.refresh(chat)
    return await chat_out(session, chat)


@router.post("/{task_id}/chat/recover", response_model=TaskChatOut)
async def recover_task_chat(
    task_id: uuid.UUID,
    actor: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> TaskChatOut:
    task = await task_for_manager(session, task_id, actor)
    chat = await session.scalar(select(TaskChat).where(TaskChat.task_id == task.id))
    if not chat or not chat.telegram_chat_id:
        raise HTTPException(
            status_code=422, detail="No existing working group is available to recover"
        )
    if chat.status == ChatStatus.READY:
        raise HTTPException(status_code=422, detail="Working group is already active")
    chat.status = ChatStatus.CREATING
    chat.last_error = None
    session.add(
        OutboxEvent(
            event_type="TASK_CHAT_RECOVERY_REQUESTED",
            aggregate_type="task_chat",
            aggregate_id=str(chat.id),
            payload={"task_id": str(task.id)},
        )
    )
    await audit(session, actor.id, "task.chat_recovery_requested", "task", task.id)
    await session.commit()
    await session.refresh(chat)
    return await chat_out(session, chat)


@router.patch("/{task_id}", response_model=TaskOut)
async def update_task(
    task_id: uuid.UUID,
    body: TaskUpdate,
    actor: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> Task:
    task = await task_for_manager(session, task_id, actor)
    if task.status not in {TaskStatus.ACTIVE, TaskStatus.OVERDUE, TaskStatus.RETURNED}:
        raise HTTPException(status_code=422, detail="Only open tasks can be changed")
    changes = body.model_dump(exclude_unset=True)
    if "deadline" in changes and changes["deadline"] <= datetime.now(UTC):
        raise HTTPException(status_code=422, detail="Deadline must be in the future")
    if "leader_id" in changes and changes["leader_id"]:
        await require_active_user(session, changes["leader_id"])
        leader = await session.scalar(
            select(TaskMember).where(
                TaskMember.task_id == task.id, TaskMember.user_id == changes["leader_id"]
            )
        )
        if not leader:
            raise HTTPException(status_code=422, detail="Leader must be a task member")
        for member in (
            await session.scalars(select(TaskMember).where(TaskMember.task_id == task.id))
        ).all():
            member.is_leader = member.user_id == changes["leader_id"]
    for key, value in changes.items():
        setattr(task, key, value.strip() if key == "title" and value else value)
    members = {
        member.user_id
        for member in (
            await session.scalars(select(TaskMember).where(TaskMember.task_id == task.id))
        ).all()
    }
    await queue_task_notifications(session, task, members, "TASK_UPDATED")
    await audit(session, actor.id, "task.updated", "task", task.id, changes)
    await session.commit()
    await session.refresh(task)
    return task


@router.get("/{task_id}/members", response_model=list[TaskMemberOut])
async def list_task_members(
    task_id: uuid.UUID,
    actor: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> list[TaskMemberOut]:
    if not await is_task_member(session, task_id, actor.id):
        raise HTTPException(status_code=403, detail="Task membership required")
    rows = (
        await session.execute(
            select(TaskMember, User).join(User).where(TaskMember.task_id == task_id)
        )
    ).all()
    return [
        TaskMemberOut(
            user=UserOut.model_validate(user, from_attributes=True),
            is_creator=member.is_creator,
            is_leader=member.is_leader,
        )
        for member, user in rows
    ]


@router.post("/{task_id}/members", response_model=TaskMemberOut, status_code=201)
async def add_task_member(
    task_id: uuid.UUID,
    body: TaskMemberCreate,
    actor: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> TaskMemberOut:
    task = await task_for_manager(session, task_id, actor)
    if task.status not in {TaskStatus.ACTIVE, TaskStatus.OVERDUE, TaskStatus.RETURNED}:
        raise HTTPException(status_code=422, detail="Only open tasks can receive members")
    if task.kind.value == "individual":
        raise HTTPException(status_code=422, detail="Individual task membership is fixed")
    user = await require_active_user(session, body.user_id)
    if actor.role == Role.SECTOR_HEAD and user.sector_id != task.sector_id:
        raise HTTPException(status_code=403, detail="You can assign only users in your sector")
    if await session.scalar(
        select(TaskMember).where(TaskMember.task_id == task.id, TaskMember.user_id == user.id)
    ):
        raise HTTPException(status_code=409, detail="User is already a task member")
    member = TaskMember(task_id=task.id, user_id=user.id)
    session.add(member)
    await queue_task_notifications(session, task, {user.id}, "TASK_ASSIGNED")
    chat = await session.scalar(select(TaskChat).where(TaskChat.task_id == task.id))
    if chat and chat.status == ChatStatus.READY and chat.telegram_chat_id:
        session.add(
            OutboxEvent(
                event_type="TASK_CHAT_MEMBER_INVITE_REQUESTED",
                aggregate_type="task_chat",
                aggregate_id=str(chat.id),
                payload={"task_id": str(task.id), "user_id": str(user.id)},
            )
        )
    await audit(session, actor.id, "task.member_added", "task", task.id, {"user_id": str(user.id)})
    await session.commit()
    return TaskMemberOut(
        user=UserOut.model_validate(user, from_attributes=True), is_creator=False, is_leader=False
    )


@router.post("/{task_id}/chat/members/{user_id}/retry", response_model=TaskChatOut)
async def retry_task_chat_member(
    task_id: uuid.UUID,
    user_id: uuid.UUID,
    actor: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> TaskChatOut:
    task = await task_for_manager(session, task_id, actor)
    chat = await session.scalar(select(TaskChat).where(TaskChat.task_id == task.id))
    if not chat or chat.status != ChatStatus.READY or not chat.telegram_chat_id:
        raise HTTPException(status_code=422, detail="Working group is not ready")
    if not await session.scalar(
        select(TaskMember).where(TaskMember.task_id == task.id, TaskMember.user_id == user_id)
    ):
        raise HTTPException(status_code=404, detail="Task member not found")
    member = await session.scalar(
        select(TaskChatMember).where(
            TaskChatMember.task_chat_id == chat.id, TaskChatMember.user_id == user_id
        )
    )
    if member and member.state == MembershipState.JOINED:
        raise HTTPException(status_code=422, detail="Member has already joined the working group")
    session.add(
        OutboxEvent(
            event_type="TASK_CHAT_MEMBER_INVITE_REQUESTED",
            aggregate_type="task_chat",
            aggregate_id=str(chat.id),
            payload={"task_id": str(task.id), "user_id": str(user_id)},
        )
    )
    await audit(
        session,
        actor.id,
        "task.chat_member_retry_requested",
        "task",
        task.id,
        {"user_id": str(user_id)},
    )
    await session.commit()
    return await chat_out(session, chat)


@router.delete("/{task_id}/members/{user_id}", status_code=204)
async def remove_task_member(
    task_id: uuid.UUID,
    user_id: uuid.UUID,
    actor: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> None:
    task = await task_for_manager(session, task_id, actor)
    member = await session.scalar(
        select(TaskMember).where(TaskMember.task_id == task.id, TaskMember.user_id == user_id)
    )
    if not member:
        raise HTTPException(status_code=404, detail="Task member not found")
    if member.is_creator or member.is_leader:
        raise HTTPException(status_code=422, detail="Creator or leader cannot be removed")
    await session.delete(member)
    await audit(
        session, actor.id, "task.member_removed", "task", task.id, {"user_id": str(user_id)}
    )
    await session.commit()


@router.post("/{task_id}/checklist", response_model=ChecklistItemOut, status_code=201)
async def add_checklist_item(
    task_id: uuid.UUID,
    body: ChecklistItemCreate,
    actor: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> TaskChecklistItem:
    task = await task_for_manager(session, task_id, actor)
    if task.status not in {TaskStatus.ACTIVE, TaskStatus.OVERDUE, TaskStatus.RETURNED}:
        raise HTTPException(status_code=422, detail="Only open tasks can be changed")
    position = len(
        (
            await session.scalars(
                select(TaskChecklistItem).where(TaskChecklistItem.task_id == task.id)
            )
        ).all()
    )
    item = TaskChecklistItem(task_id=task.id, title=body.title.strip(), position=position)
    session.add(item)
    members = {
        member.user_id
        for member in (
            await session.scalars(select(TaskMember).where(TaskMember.task_id == task.id))
        ).all()
    }
    await queue_task_notifications(session, task, members, "TASK_UPDATED")
    await audit(session, actor.id, "task.checklist_added", "task", task.id, {"item": item.title})
    await session.commit()
    await session.refresh(item)
    return item


@router.delete("/{task_id}/checklist/{item_id}", status_code=204)
async def delete_checklist_item(
    task_id: uuid.UUID,
    item_id: uuid.UUID,
    actor: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> None:
    task = await task_for_manager(session, task_id, actor)
    item = await session.get(TaskChecklistItem, item_id)
    if not item or item.task_id != task.id:
        raise HTTPException(status_code=404, detail="Checklist item not found")
    await session.delete(item)
    await audit(
        session, actor.id, "task.checklist_removed", "task", task.id, {"item_id": str(item_id)}
    )
    await session.commit()


@router.post("/{task_id}/cancel", response_model=TaskOut)
async def cancel_task(
    task_id: uuid.UUID,
    actor: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> Task:
    task = await task_for_manager(session, task_id, actor)
    if task.status in {TaskStatus.COMPLETED, TaskStatus.CANCELLED}:
        raise HTTPException(status_code=422, detail="Task is already closed")
    task.status = TaskStatus.CANCELLED
    task.cancelled_at = datetime.now(UTC)
    task.cleanup_at = task_cleanup_at(task.cancelled_at, task.deadline)
    await refresh_event_retention(session, task.event_id)
    members = {
        member.user_id
        for member in (
            await session.scalars(select(TaskMember).where(TaskMember.task_id == task.id))
        ).all()
    }
    await queue_task_notifications(session, task, members, "TASK_CANCELLED")
    await audit(session, actor.id, "task.cancelled", "task", task.id)
    await session.commit()
    await session.refresh(task)
    return task


@router.patch("/{task_id}/checklist/{item_id}")
async def update_checklist(
    task_id: uuid.UUID,
    item_id: uuid.UUID,
    body: ChecklistUpdate,
    actor: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    if not await is_task_member(session, task_id, actor.id):
        raise HTTPException(status_code=403, detail="Task membership required")
    task = await session.get(Task, task_id)
    if not task or task.status not in {TaskStatus.ACTIVE, TaskStatus.OVERDUE, TaskStatus.RETURNED}:
        raise HTTPException(
            status_code=422, detail="Checklist can be updated only for an open task"
        )
    item = await session.get(TaskChecklistItem, item_id)
    if not item or item.task_id != task_id:
        raise HTTPException(status_code=404, detail="Checklist item not found")
    item.is_completed = body.is_completed
    item.completed_by_id = actor.id if body.is_completed else None
    item.completed_at = datetime.now(UTC) if body.is_completed else None
    await audit(session, actor.id, "task.checklist_updated", "task_checklist_item", item.id)
    await session.commit()
    return {"id": str(item.id), "is_completed": item.is_completed}
