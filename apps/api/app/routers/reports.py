import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..db import get_session
from ..dependencies import current_user
from ..models import (
    OutboxEvent,
    ReportStatus,
    Task,
    TaskMember,
    TaskPhoto,
    TaskReport,
    TaskStatus,
    User,
)
from ..photos import inspect_photo
from ..schemas import (
    PhotoComplete,
    PhotoOut,
    ReportCreate,
    ReportDecision,
    ReportOut,
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
from ..storage import (
    create_presigned_download,
    create_presigned_upload,
    delete_object,
    get_object_bytes,
    object_metadata,
    put_object,
)

router = APIRouter(prefix="/tasks/{task_id}", tags=["reports"])
_EDITABLE_TASK_STATUSES = {TaskStatus.ACTIVE, TaskStatus.RETURNED, TaskStatus.OVERDUE}
_EDITABLE_REPORT_STATUSES = {ReportStatus.DRAFT, ReportStatus.RETURNED}


async def require_task_member(
    session: AsyncSession, task_id: uuid.UUID, actor: User
) -> Task:
    if not await is_task_member(session, task_id, actor.id):
        raise HTTPException(status_code=403, detail="Task membership required")
    task = await session.get(Task, task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    return task


async def get_or_create_editable_report(
    session: AsyncSession, task: Task, actor: User, *, lock: bool = False
) -> TaskReport:
    statement = select(TaskReport).where(TaskReport.task_id == task.id)
    if lock:
        statement = statement.with_for_update()
    report = await session.scalar(statement)
    if report:
        if report.status not in _EDITABLE_REPORT_STATUSES:
            raise HTTPException(status_code=422, detail="Submitted report can no longer be edited")
        return report
    report = TaskReport(
        task_id=task.id,
        submitted_by_id=actor.id,
        status=ReportStatus.DRAFT,
    )
    session.add(report)
    await session.flush()
    return report


def photo_out(photo: TaskPhoto) -> PhotoOut:
    original_url = None
    preview_url = None
    try:
        original_url = create_presigned_download(photo.object_key)
        if photo.preview_object_key:
            preview_url = create_presigned_download(photo.preview_object_key)
    except Exception:
        # Metadata remains usable if storage is temporarily unavailable.
        pass
    return PhotoOut(
        id=photo.id,
        content_type=photo.content_type,
        size_bytes=photo.size_bytes,
        preview_object_key=photo.preview_object_key,
        width=photo.width,
        height=photo.height,
        original_url=original_url,
        preview_url=preview_url,
    )


async def report_out(session: AsyncSession, report: TaskReport) -> ReportOut:
    photos = list(
        (
            await session.scalars(
                select(TaskPhoto)
                .where(TaskPhoto.report_id == report.id)
                .order_by(TaskPhoto.created_at)
            )
        ).all()
    )
    return ReportOut(
        id=report.id,
        task_id=report.task_id,
        submitted_by_id=report.submitted_by_id,
        status=report.status,
        comment=report.comment,
        approval_comment=report.approval_comment,
        submitted_at=report.submitted_at,
        returned_at=report.returned_at,
        approved_at=report.approved_at,
        photos=[photo_out(photo) for photo in photos],
    )


@router.get("/report", response_model=ReportOut)
async def get_report(
    task_id: uuid.UUID,
    actor: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> ReportOut:
    await require_task_member(session, task_id, actor)
    report = await session.scalar(select(TaskReport).where(TaskReport.task_id == task_id))
    if not report:
        raise HTTPException(status_code=404, detail="Task has no report yet")
    return await report_out(session, report)


@router.post("/report", response_model=ReportOut, status_code=201)
async def submit_report(
    task_id: uuid.UUID,
    body: ReportCreate,
    actor: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> ReportOut:
    task = await require_task_member(session, task_id, actor)
    if task.status not in _EDITABLE_TASK_STATUSES:
        raise HTTPException(status_code=422, detail="Task cannot accept a report")

    report = await get_or_create_editable_report(session, task, actor, lock=True)
    now = datetime.now(UTC)
    report.submitted_by_id = actor.id
    report.comment = body.comment
    report.submitted_at = now
    report.returned_at = None
    report.approval_comment = None

    recipients = {
        member.user_id
        for member in (
            await session.scalars(select(TaskMember).where(TaskMember.task_id == task.id))
        ).all()
    }
    if task.kind.value == "group":
        report.status = ReportStatus.SUBMITTED
        report.approved_at = None
        task.status = TaskStatus.SUBMITTED
        await queue_task_notifications(session, task, recipients, "TASK_SUBMITTED")
    else:
        report.status = ReportStatus.APPROVED
        report.approved_at = now
        task.status = TaskStatus.COMPLETED
        task.completed_at = now
        task.cleanup_at = task_cleanup_at(now, task.deadline)
        await refresh_event_retention(session, task.event_id)
        session.add(
            OutboxEvent(
                event_type="TASK_COMPLETED",
                aggregate_type="task",
                aggregate_id=str(task.id),
                payload={"task_id": str(task.id)},
            )
        )
        await queue_task_notifications(session, task, recipients, "TASK_COMPLETED")

    await audit(session, actor.id, "task.report_submitted", "task", task.id)
    await session.commit()
    await session.refresh(report)
    return await report_out(session, report)


@router.post("/report/upload", response_model=UploadTarget)
async def request_photo_upload(
    task_id: uuid.UUID,
    body: UploadRequest,
    actor: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> UploadTarget:
    task = await require_task_member(session, task_id, actor)
    if task.status not in _EDITABLE_TASK_STATUSES:
        raise HTTPException(status_code=422, detail="Photos can be changed only before report submission")
    if body.content_type not in {"image/jpeg", "image/png", "image/webp"}:
        raise HTTPException(status_code=422, detail="Only JPEG, PNG, and WebP photos are allowed")

    report = await get_or_create_editable_report(session, task, actor, lock=True)
    count = await session.scalar(
        select(func.count()).select_from(TaskPhoto).where(TaskPhoto.report_id == report.id)
    )
    if count >= 5:
        raise HTTPException(status_code=422, detail="A report can have at most 5 photos")
    try:
        object_key, url = create_presigned_upload(task_id, body.filename, body.content_type)
    except Exception as exc:
        raise HTTPException(status_code=503, detail="Photo storage is temporarily unavailable") from exc
    await session.commit()
    return UploadTarget(object_key=object_key, upload_url=url)


@router.post("/report/photos/complete", response_model=PhotoOut, status_code=201)
async def confirm_photo_upload(
    task_id: uuid.UUID,
    body: PhotoComplete,
    actor: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> PhotoOut:
    task = await require_task_member(session, task_id, actor)
    if task.status not in _EDITABLE_TASK_STATUSES:
        raise HTTPException(status_code=422, detail="Photos can be changed only before report submission")
    if not body.object_key.startswith(f"tasks/{task_id}/"):
        raise HTTPException(status_code=422, detail="Invalid report photo")

    report = await get_or_create_editable_report(session, task, actor, lock=True)
    count = await session.scalar(
        select(func.count()).select_from(TaskPhoto).where(TaskPhoto.report_id == report.id)
    )
    if count >= 5:
        raise HTTPException(status_code=422, detail="A report can have at most 5 photos")
    if await session.scalar(select(TaskPhoto.id).where(TaskPhoto.object_key == body.object_key)):
        raise HTTPException(status_code=409, detail="Photo is already attached")

    try:
        metadata = object_metadata(body.object_key)
        if metadata.size_bytes <= 0 or metadata.size_bytes > 10 * 1024 * 1024:
            raise HTTPException(
                status_code=422, detail="Photo size must be between 1 byte and 10 MB"
            )
        processed = inspect_photo(get_object_bytes(body.object_key))
        preview_key = f"previews/{task_id}/{uuid.uuid4()}.jpg"
        put_object(preview_key, processed.preview_bytes, "image/jpeg")
    except HTTPException:
        raise
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="Stored object is not a valid image") from exc
    except Exception as exc:
        raise HTTPException(status_code=503, detail="Could not verify photo storage") from exc

    photo = TaskPhoto(
        report_id=report.id,
        object_key=body.object_key,
        content_type=processed.content_type,
        size_bytes=metadata.size_bytes,
        preview_object_key=preview_key,
        width=processed.width,
        height=processed.height,
        uploaded_by_id=actor.id,
    )
    session.add(photo)
    await session.commit()
    await session.refresh(photo)
    return photo_out(photo)


@router.delete("/report/photos/{photo_id}", status_code=204)
async def delete_report_photo(
    task_id: uuid.UUID,
    photo_id: uuid.UUID,
    actor: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> None:
    task = await require_task_member(session, task_id, actor)
    if task.status not in _EDITABLE_TASK_STATUSES:
        raise HTTPException(status_code=422, detail="Photos can be changed only before report submission")
    report = await session.scalar(
        select(TaskReport).where(TaskReport.task_id == task_id).with_for_update()
    )
    if not report or report.status not in _EDITABLE_REPORT_STATUSES:
        raise HTTPException(status_code=422, detail="Report is not editable")
    photo = await session.get(TaskPhoto, photo_id)
    if not photo or photo.report_id != report.id:
        raise HTTPException(status_code=404, detail="Report photo not found")
    try:
        delete_object(photo.object_key)
        if photo.preview_object_key:
            delete_object(photo.preview_object_key)
    except Exception as exc:
        raise HTTPException(status_code=503, detail="Could not remove photo from storage") from exc
    await session.delete(photo)
    await audit(session, actor.id, "task.report_photo_deleted", "task", task.id)
    await session.commit()


@router.post("/report/decision", response_model=ReportOut)
async def decide_report(
    task_id: uuid.UUID,
    body: ReportDecision,
    actor: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> ReportOut:
    task = await session.get(Task, task_id)
    if not task or task.status != TaskStatus.SUBMITTED:
        raise HTTPException(status_code=422, detail="No submitted report awaits approval")
    if task.leader_id != actor.id:
        raise HTTPException(status_code=403, detail="Only the task leader can decide")
    if not body.approved and not body.reason:
        raise HTTPException(status_code=422, detail="A rework reason is required")

    report = await session.scalar(
        select(TaskReport).where(TaskReport.task_id == task_id).with_for_update()
    )
    if not report or report.status != ReportStatus.SUBMITTED:
        raise HTTPException(status_code=422, detail="Submitted report record is missing")

    now = datetime.now(UTC)
    recipients = {
        member.user_id
        for member in (
            await session.scalars(select(TaskMember).where(TaskMember.task_id == task.id))
        ).all()
    }
    report.approval_comment = body.reason
    if body.approved:
        task.status = TaskStatus.COMPLETED
        task.completed_at = now
        task.cleanup_at = task_cleanup_at(now, task.deadline)
        report.status = ReportStatus.APPROVED
        report.approved_at = now
        report.returned_at = None
        await refresh_event_retention(session, task.event_id)
        session.add(
            OutboxEvent(
                event_type="TASK_COMPLETED",
                aggregate_type="task",
                aggregate_id=str(task.id),
                payload={"task_id": str(task.id)},
            )
        )
        await queue_task_notifications(session, task, recipients, "TASK_COMPLETED")
    else:
        task.status = TaskStatus.RETURNED
        report.status = ReportStatus.RETURNED
        report.returned_at = now
        report.approved_at = None
        await queue_task_notifications(session, task, recipients, "TASK_UPDATED")

    await audit(
        session,
        actor.id,
        "task.report_approved" if body.approved else "task.report_returned",
        "task",
        task.id,
        {"reason": body.reason},
    )
    await session.commit()
    await session.refresh(report)
    return await report_out(session, report)
