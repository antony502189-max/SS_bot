import uuid

from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..archive import build_event_archive_pdf, build_photo_zip
from ..db import get_session
from ..dependencies import current_user
from ..models import (
    Event,
    EventParticipant,
    ReportStatus,
    Role,
    Task,
    TaskMember,
    TaskPhoto,
    TaskReport,
    User,
)
from ..schemas import (
    ArchiveReportOut,
    ArchiveTaskOut,
    EventArchiveOut,
    EventCreate,
    EventOut,
    EventParticipantCreate,
    EventUpdate,
    RetentionExtend,
    UserOut,
)
from ..services import audit, create_event, ensure_sector_access, require_active_user, require_role

router = APIRouter(prefix="/events", tags=["events"])


@router.get("", response_model=list[EventOut])
async def list_events(
    actor: User = Depends(current_user), session: AsyncSession = Depends(get_session)
) -> list[Event]:
    await require_role(session, actor.id, Role.ADMIN, Role.SECTOR_HEAD)
    statement = select(Event).where(Event.purged_at.is_(None)).order_by(Event.starts_at.desc())
    if actor.role == Role.SECTOR_HEAD:
        statement = statement.where(Event.sector_id == actor.sector_id)
    return list((await session.scalars(statement)).all())


@router.post("", response_model=EventOut, status_code=201)
async def create_event_route(
    body: EventCreate,
    actor: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> Event:
    await require_role(session, actor.id, Role.ADMIN, Role.SECTOR_HEAD)
    event = await create_event(session, actor, body)
    await session.commit()
    await session.refresh(event)
    return event


async def event_for_manager(session: AsyncSession, event_id: uuid.UUID, actor: User) -> Event:
    await require_role(session, actor.id, Role.ADMIN, Role.SECTOR_HEAD)
    event = await session.get(Event, event_id)
    if not event:
        raise HTTPException(status_code=404, detail="Event not found")
    ensure_sector_access(actor, event.sector_id)
    return event


async def archive_out(session: AsyncSession, event: Event) -> EventArchiveOut:
    explicit_participants = list(
        (
            await session.scalars(
                select(User).join(EventParticipant).where(EventParticipant.event_id == event.id)
            )
        ).all()
    )
    task_participants = list(
        (
            await session.scalars(
                select(User)
                .join(TaskMember, TaskMember.user_id == User.id)
                .join(Task, Task.id == TaskMember.task_id)
                .where(Task.event_id == event.id)
            )
        ).unique().all()
    )
    participants_by_id = {user.id: user for user in explicit_participants + task_participants}
    participants = sorted(
        participants_by_id.values(),
        key=lambda user: (user.full_name or "", user.telegram_username or ""),
    )

    tasks = list(
        (
            await session.scalars(
                select(Task).where(Task.event_id == event.id).order_by(Task.deadline)
            )
        ).all()
    )
    reports = (
        {
            report.task_id: report
            for report in (
                await session.scalars(
                    select(TaskReport).where(
                        TaskReport.task_id.in_([task.id for task in tasks]),
                        TaskReport.status != ReportStatus.DRAFT,
                    )
                )
            ).all()
        }
        if tasks
        else {}
    )
    photo_counts = (
        {
            report_id: count
            for report_id, count in (
                await session.execute(
                    select(TaskPhoto.report_id, func.count())
                    .where(TaskPhoto.report_id.in_([report.id for report in reports.values()]))
                    .group_by(TaskPhoto.report_id)
                )
            ).all()
        }
        if reports
        else {}
    )
    return EventArchiveOut(
        event=EventOut.model_validate(event, from_attributes=True),
        participants=[UserOut.model_validate(user, from_attributes=True) for user in participants],
        tasks=[
            ArchiveTaskOut(
                id=task.id,
                title=task.title,
                status=task.status,
                deadline=task.deadline,
                completed_at=task.completed_at,
                cancelled_at=task.cancelled_at,
                report=(
                    ArchiveReportOut(
                        submitted_by_id=report.submitted_by_id,
                        comment=report.comment,
                        approval_comment=report.approval_comment,
                        submitted_at=report.submitted_at,
                        approved_at=report.approved_at,
                        photo_count=photo_counts.get(report.id, 0),
                    )
                    if (report := reports.get(task.id))
                    else None
                ),
            )
            for task in tasks
        ],
    )


@router.get("/{event_id}", response_model=EventOut)
async def get_event(
    event_id: uuid.UUID,
    actor: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> Event:
    return await event_for_manager(session, event_id, actor)


@router.get("/{event_id}/archive", response_model=EventArchiveOut)
async def get_event_archive(
    event_id: uuid.UUID,
    actor: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> EventArchiveOut:
    event = await event_for_manager(session, event_id, actor)
    if event.purged_at:
        raise HTTPException(status_code=410, detail="Archive has passed its retention period")
    return await archive_out(session, event)


@router.get("/{event_id}/exports/pdf")
async def export_event_pdf(
    event_id: uuid.UUID,
    actor: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> Response:
    event = await event_for_manager(session, event_id, actor)
    if event.purged_at:
        raise HTTPException(status_code=410, detail="Archive has passed its retention period")
    archive = await archive_out(session, event)
    payload = build_event_archive_pdf(
        event, archive.participants, [task.model_dump() for task in archive.tasks]
    )
    await audit(session, actor.id, "event.archive_pdf_exported", "event", event.id)
    await session.commit()
    return Response(
        payload,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="event-{event.id}-archive.pdf"'},
    )


@router.get("/{event_id}/exports/photos")
async def export_event_photos(
    event_id: uuid.UUID,
    actor: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> Response:
    event = await event_for_manager(session, event_id, actor)
    if event.purged_at:
        raise HTTPException(status_code=410, detail="Archive has passed its retention period")
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
        payload = build_photo_zip(photos)
    except Exception as exc:
        raise HTTPException(
            status_code=503, detail="Photo archive is temporarily unavailable"
        ) from exc
    await audit(session, actor.id, "event.archive_photos_exported", "event", event.id)
    await session.commit()
    return Response(
        payload,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="event-{event.id}-photos.zip"'},
    )


@router.post("/{event_id}/retention/extend", response_model=EventOut)
async def extend_event_retention(
    event_id: uuid.UUID,
    body: RetentionExtend,
    actor: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> Event:
    await require_role(session, actor.id, Role.ADMIN)
    event = await session.get(Event, event_id)
    if not event or event.purged_at:
        raise HTTPException(status_code=404, detail="Active event archive not found")
    current_limit = max(
        (value for value in [event.retention_delete_at, event.retention_extended_until] if value),
        default=None,
    )
    if current_limit and body.until <= current_limit:
        raise HTTPException(status_code=422, detail="Extension must be after current retention")
    event.retention_extended_until = body.until
    event.retention_warning_sent_at = None
    await audit(
        session,
        actor.id,
        "event.retention_extended",
        "event",
        event.id,
        {"until": body.until.isoformat()},
    )
    await session.commit()
    await session.refresh(event)
    return event


@router.patch("/{event_id}", response_model=EventOut)
async def update_event(
    event_id: uuid.UUID,
    body: EventUpdate,
    actor: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> Event:
    event = await event_for_manager(session, event_id, actor)
    changes = body.model_dump(exclude_unset=True)
    target_sector = changes.get("sector_id", event.sector_id)
    if actor.role == Role.SECTOR_HEAD and target_sector is None:
        target_sector = actor.sector_id
        changes["sector_id"] = target_sector
    ensure_sector_access(actor, target_sector)
    if "title" in changes and changes["title"]:
        changes["title"] = changes["title"].strip()
    starts_at = changes.get("starts_at", event.starts_at)
    ends_at = changes.get("ends_at", event.ends_at)
    if ends_at and starts_at and ends_at < starts_at:
        raise HTTPException(status_code=422, detail="Event end cannot be before its start")
    for key, value in changes.items():
        setattr(event, key, value)
    await audit(session, actor.id, "event.updated", "event", event.id, changes)
    await session.commit()
    await session.refresh(event)
    return event


@router.get("/{event_id}/participants", response_model=list[UserOut])
async def list_participants(
    event_id: uuid.UUID,
    actor: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> list[User]:
    await event_for_manager(session, event_id, actor)
    return list(
        (
            await session.scalars(
                select(User).join(EventParticipant).where(EventParticipant.event_id == event_id)
            )
        ).all()
    )


@router.post("/{event_id}/participants", response_model=UserOut, status_code=201)
async def add_participant(
    event_id: uuid.UUID,
    body: EventParticipantCreate,
    actor: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> User:
    event = await event_for_manager(session, event_id, actor)
    user = await require_active_user(session, body.user_id)
    if actor.role == Role.SECTOR_HEAD and user.sector_id != event.sector_id:
        raise HTTPException(status_code=403, detail="You can assign only users in your sector")
    existing = await session.scalar(
        select(EventParticipant).where(
            EventParticipant.event_id == event.id, EventParticipant.user_id == user.id
        )
    )
    if existing:
        raise HTTPException(status_code=409, detail="User already participates in this event")
    session.add(EventParticipant(event_id=event.id, user_id=user.id))
    await audit(
        session,
        actor.id,
        "event.participant_added",
        "event",
        event.id,
        {"user_id": str(user.id)},
    )
    await session.commit()
    return user


@router.delete("/{event_id}/participants/{user_id}", status_code=204)
async def remove_participant(
    event_id: uuid.UUID,
    user_id: uuid.UUID,
    actor: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> None:
    event = await event_for_manager(session, event_id, actor)
    participant = await session.scalar(
        select(EventParticipant).where(
            EventParticipant.event_id == event.id,
            EventParticipant.user_id == user_id,
        )
    )
    if not participant:
        raise HTTPException(status_code=404, detail="Event participant not found")
    await session.delete(participant)
    await audit(
        session,
        actor.id,
        "event.participant_removed",
        "event",
        event.id,
        {"user_id": str(user_id)},
    )
    await session.commit()
