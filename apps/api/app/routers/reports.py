import uuid
from datetime import UTC, datetime
from io import BytesIO

from fastapi import APIRouter, Depends, HTTPException
from PIL import Image, UnidentifiedImageError
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..db import get_session
from ..dependencies import current_user
from ..models import OutboxEvent, Task, TaskMember, TaskPhoto, TaskReport, TaskStatus, User
from ..schemas import (
    PhotoComplete,
    PhotoOut,
    ReportCreate,
    ReportDecision,
    UploadRequest,
    UploadTarget,
)
from ..services import (
    audit,
    is_task_member,
    queue_task_notifications,
    refresh_event_retention,
    task_cleanup_at,
)
from ..storage import create_presigned_upload, get_object_bytes, object_metadata, put_object

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
        await refresh_event_retention(session, task.event_id)
        session.add(
            OutboxEvent(
                event_type="TASK_COMPLETED",
                aggregate_type="task",
                aggregate_id=str(task.id),
                payload={"task_id": str(task.id)},
            )
        )
    session.add(report)
    recipients = {
        member.user_id
        for member in (
            await session.scalars(select(TaskMember).where(TaskMember.task_id == task.id))
        ).all()
    }
    await queue_task_notifications(session, task, recipients, "TASK_SUBMITTED")
    if task.status == TaskStatus.COMPLETED:
        await queue_task_notifications(session, task, recipients, "TASK_COMPLETED")
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


@router.post("/report/photos/complete", response_model=PhotoOut, status_code=201)
async def confirm_photo_upload(
    task_id: uuid.UUID,
    body: PhotoComplete,
    actor: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> TaskPhoto:
    if not await is_task_member(session, task_id, actor.id):
        raise HTTPException(status_code=403, detail="Task membership required")
    report = await session.scalar(select(TaskReport).where(TaskReport.task_id == task_id))
    if not report or not body.object_key.startswith(f"tasks/{task_id}/"):
        raise HTTPException(status_code=422, detail="Invalid report photo")
    count = await session.scalar(
        select(func.count()).select_from(TaskPhoto).where(TaskPhoto.report_id == report.id)
    )
    if count >= 5:
        raise HTTPException(status_code=422, detail="A report can have at most 5 photos")
    try:
        metadata = object_metadata(body.object_key)
        if metadata.size_bytes <= 0 or metadata.size_bytes > 10 * 1024 * 1024:
            raise HTTPException(
                status_code=422, detail="Photo size must be between 1 byte and 10 MB"
            )
        raw = get_object_bytes(body.object_key)
        with Image.open(BytesIO(raw)) as image:
            image.verify()
        with Image.open(BytesIO(raw)) as image:
            if image.format not in {"JPEG", "PNG", "WEBP"}:
                raise HTTPException(
                    status_code=422, detail="Only JPEG, PNG, and WebP photos are allowed"
                )
            actual_content_type = {
                "JPEG": "image/jpeg",
                "PNG": "image/png",
                "WEBP": "image/webp",
            }[image.format]
            width, height = image.size
            preview = image.convert("RGB")
            preview.thumbnail((960, 960))
            output = BytesIO()
            preview.save(output, format="JPEG", quality=82, optimize=True)
        preview_key = f"previews/{task_id}/{uuid.uuid4()}.jpg"
        put_object(preview_key, output.getvalue(), "image/jpeg")
    except HTTPException:
        raise
    except (UnidentifiedImageError, OSError) as exc:
        raise HTTPException(status_code=422, detail="Stored object is not a valid image") from exc
    except Exception as exc:
        raise HTTPException(status_code=503, detail="Could not verify photo storage") from exc
    photo = TaskPhoto(
        report_id=report.id,
        object_key=body.object_key,
        content_type=actual_content_type,
        size_bytes=metadata.size_bytes,
        preview_object_key=preview_key,
        width=width,
        height=height,
        uploaded_by_id=actor.id,
    )
    session.add(photo)
    await session.commit()
    return photo


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
        await refresh_event_retention(session, task.event_id)
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
    recipients = {
        member.user_id
        for member in (
            await session.scalars(select(TaskMember).where(TaskMember.task_id == task.id))
        ).all()
    }
    await queue_task_notifications(
        session, task, recipients, "TASK_COMPLETED" if body.approved else "TASK_UPDATED"
    )
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
