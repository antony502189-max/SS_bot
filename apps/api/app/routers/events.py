import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..db import get_session
from ..dependencies import current_user
from ..models import Event, EventParticipant, Role, User
from ..schemas import EventCreate, EventOut, EventParticipantCreate, EventUpdate, UserOut
from ..services import audit, create_event, ensure_sector_access, require_active_user, require_role

router = APIRouter(prefix="/events", tags=["events"])


@router.get("", response_model=list[EventOut])
async def list_events(
    actor: User = Depends(current_user), session: AsyncSession = Depends(get_session)
) -> list[Event]:
    statement = select(Event).order_by(Event.starts_at.desc())
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
    event = await session.get(Event, event_id)
    if not event:
        raise HTTPException(status_code=404, detail="Event not found")
    ensure_sector_access(actor, event.sector_id)
    return event


@router.get("/{event_id}", response_model=EventOut)
async def get_event(
    event_id: uuid.UUID,
    actor: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> Event:
    await require_role(session, actor.id, Role.ADMIN, Role.SECTOR_HEAD)
    return await event_for_manager(session, event_id, actor)


@router.patch("/{event_id}", response_model=EventOut)
async def update_event(
    event_id: uuid.UUID,
    body: EventUpdate,
    actor: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> Event:
    await require_role(session, actor.id, Role.ADMIN, Role.SECTOR_HEAD)
    event = await event_for_manager(session, event_id, actor)
    changes = body.model_dump(exclude_unset=True)
    target_sector = changes.get("sector_id", event.sector_id)
    ensure_sector_access(actor, target_sector)
    if "title" in changes and changes["title"]:
        changes["title"] = changes["title"].strip()
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
    await require_role(session, actor.id, Role.ADMIN, Role.SECTOR_HEAD)
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
    await require_role(session, actor.id, Role.ADMIN, Role.SECTOR_HEAD)
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
        session, actor.id, "event.participant_added", "event", event.id, {"user_id": str(user.id)}
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
    await require_role(session, actor.id, Role.ADMIN, Role.SECTOR_HEAD)
    event = await event_for_manager(session, event_id, actor)
    participant = await session.scalar(
        select(EventParticipant).where(
            EventParticipant.event_id == event.id, EventParticipant.user_id == user_id
        )
    )
    if not participant:
        raise HTTPException(status_code=404, detail="Event participant not found")
    await session.delete(participant)
    await audit(
        session, actor.id, "event.participant_removed", "event", event.id, {"user_id": str(user_id)}
    )
    await session.commit()
