import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Response
from sqlalchemy.ext.asyncio import AsyncSession

from ..db import get_session
from ..models import Role, Task, TaskChecklistItem
from ..schemas import ChecklistUpdate, TaskCreate, TaskOut
from ..services import (
    audit,
    create_task,
    is_task_member,
    require_active_user,
    require_role,
    task_query_for_user,
)

router = APIRouter(prefix="/tasks", tags=["tasks"])


@router.get("", response_model=list[TaskOut])
async def my_tasks(
    user_id: uuid.UUID = Query(...), session: AsyncSession = Depends(get_session)
) -> list[Task]:
    await require_active_user(session, user_id)
    return list((await session.scalars(await task_query_for_user(user_id))).unique().all())


@router.post("", response_model=TaskOut, status_code=201)
async def create_task_route(
    body: TaskCreate,
    response: Response,
    actor_id: uuid.UUID = Query(...),
    idempotency_key: str = Header(alias="Idempotency-Key", default=""),
    session: AsyncSession = Depends(get_session),
) -> Task:
    actor = await require_role(session, actor_id, Role.ADMIN, Role.SECTOR_HEAD)
    task, reused = await create_task(session, actor, body, idempotency_key)
    if reused:
        response.status_code = 200
        return task
    await session.commit()
    await session.refresh(task)
    return task


@router.get("/{task_id}", response_model=TaskOut)
async def get_task(
    task_id: uuid.UUID,
    user_id: uuid.UUID = Query(...),
    session: AsyncSession = Depends(get_session),
) -> Task:
    if not await is_task_member(session, task_id, user_id):
        raise HTTPException(status_code=403, detail="Task membership required")
    task = await session.get(Task, task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    return task


@router.patch("/{task_id}/checklist/{item_id}")
async def update_checklist(
    task_id: uuid.UUID,
    item_id: uuid.UUID,
    body: ChecklistUpdate,
    user_id: uuid.UUID = Query(...),
    session: AsyncSession = Depends(get_session),
) -> dict:
    if not await is_task_member(session, task_id, user_id):
        raise HTTPException(status_code=403, detail="Task membership required")
    item = await session.get(TaskChecklistItem, item_id)
    if not item or item.task_id != task_id:
        raise HTTPException(status_code=404, detail="Checklist item not found")
    item.is_completed = body.is_completed
    item.completed_by_id = user_id if body.is_completed else None
    item.completed_at = datetime.now(UTC) if body.is_completed else None
    await audit(session, user_id, "task.checklist_updated", "task_checklist_item", item.id)
    await session.commit()
    return {"id": str(item.id), "is_completed": item.is_completed}
