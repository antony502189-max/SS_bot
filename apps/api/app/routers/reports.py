import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..db import get_session
from ..dependencies import current_user
from ..models import OutboxEvent, Task, TaskPhoto, TaskReport, TaskStatus, User
from ..schemas import ReportCreate, ReportDecision, UploadRequest, UploadTarget
from ..services import audit, is_task_member, task_cleanup_at
from ..storage import create_presigned_upload, object_exists

router = APIRouter(prefix="/tasks/{task_id}", tags=["reports"])


@router.post("/report", status_code=201)
async def submit_report(
    task_id: uuid.UUID,
    body: ReportCreate,
    actor: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    if not await is_task_member(session, task_id, actor.id):
        raise HTTPException(status_code=403, detail="Task membership required")
    task = await session.get(Task, task_id)
    if not task or task.status not in {TaskStatus.ACTIVE, TaskStatus.RETURNED, TaskStatus.OVERDUE}:
        raise HTTPException(status_code=422, detail="Task cannot accept a report")
    report = await session.scalar(select(TaskReport).where(TaskReport.task_id == task_id))
    if report:
        raise HTTPException(status_code=409, detail="A report is already submitted")
    report = TaskReport(task_id=task_id, submitted_by_id=actor.id, comment=body.comment)
    task.status = TaskStatus.SUBMITTED if task.kind.value == "group" else TaskStatus.COMPLETED
    if task.status == TaskStatus.COMPLETED:
        task.completed_at = datetime.now(UTC)
        task.cleanup_at = task_cleanup_at(task.completed_at, task.deadline)
        session.add(
            OutboxEvent(
                event_type="TASK_COMPLETED",
                aggregate_type="task",
                aggregate_id=str(task.id),
                payload={"task_id": str(task.id)},
            )
        )
    session.add(report)
    await audit(session, actor.id, "task.report_submitted", "task", task.id)
    await session.commit()
    return {"report_id": str(report.id), "status": task.status.value}


@router.post("/report/upload", response_model=UploadTarget)
async def request_photo_upload(
    task_id: uuid.UUID,
    body: UploadRequest,
    actor: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> UploadTarget:
    if not await is_task_member(session, task_id, actor.id):
        raise HTTPException(status_code=403, detail="Task membership required")
    report = await session.scalar(select(TaskReport).where(TaskReport.task_id == task_id))
    if not report:
        raise HTTPException(status_code=422, detail="Submit a report before adding photos")
    if body.content_type not in {"image/jpeg", "image/png", "image/webp"}:
        raise HTTPException(status_code=422, detail="Only JPEG, PNG, and WebP photos are allowed")
    count = await session.scalar(
        select(func.count()).select_from(TaskPhoto).where(TaskPhoto.report_id == report.id)
    )
    if count >= 5:
        raise HTTPException(status_code=422, detail="A report can have at most 5 photos")
    object_key, url = create_presigned_upload(task_id, body.filename, body.content_type)
    return UploadTarget(object_key=object_key, upload_url=url)


@router.post("/report/photos/complete", status_code=201)
async def confirm_photo_upload(
    task_id: uuid.UUID,
    object_key: str,
    content_type: str,
    size_bytes: int,
    actor: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    if not await is_task_member(session, task_id, actor.id):
        raise HTTPException(status_code=403, detail="Task membership required")
    report = await session.scalar(select(TaskReport).where(TaskReport.task_id == task_id))
    if not report or not object_key.startswith(f"tasks/{task_id}/"):
        raise HTTPException(status_code=422, detail="Invalid report photo")
    count = await session.scalar(
        select(func.count()).select_from(TaskPhoto).where(TaskPhoto.report_id == report.id)
    )
    if count >= 5:
        raise HTTPException(status_code=422, detail="A report can have at most 5 photos")
    try:
        stored = object_exists(object_key)
    except Exception as exc:
        raise HTTPException(status_code=503, detail="Could not verify photo storage") from exc
    if not stored:
        raise HTTPException(status_code=422, detail="Photo has not been stored")
    photo = TaskPhoto(
        report_id=report.id,
        object_key=object_key,
        content_type=content_type,
        size_bytes=size_bytes,
        uploaded_by_id=actor.id,
    )
    session.add(photo)
    await session.commit()
    return {"photo_id": str(photo.id)}


@router.post("/report/decision")
async def decide_report(
    task_id: uuid.UUID,
    body: ReportDecision,
    actor: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    task = await session.get(Task, task_id)
    if not task or task.status != TaskStatus.SUBMITTED:
        raise HTTPException(status_code=422, detail="No submitted report awaits approval")
    if task.leader_id != actor.id:
        raise HTTPException(status_code=403, detail="Only the task leader can decide")
    if not body.approved and not body.reason:
        raise HTTPException(status_code=422, detail="A rework reason is required")
    report = await session.scalar(select(TaskReport).where(TaskReport.task_id == task_id))
    report.approval_comment = body.reason
    if body.approved:
        task.status = TaskStatus.COMPLETED
        task.completed_at = datetime.now(UTC)
        task.cleanup_at = task_cleanup_at(task.completed_at, task.deadline)
        report.approved_at = task.completed_at
        session.add(
            OutboxEvent(
                event_type="TASK_COMPLETED",
                aggregate_type="task",
                aggregate_id=str(task.id),
                payload={"task_id": str(task.id)},
            )
        )
    else:
        task.status = TaskStatus.RETURNED
    await audit(
        session,
        actor.id,
        "task.report_approved" if body.approved else "task.report_returned",
        "task",
        task.id,
        {"reason": body.reason},
    )
    await session.commit()
    return {"status": task.status.value}
