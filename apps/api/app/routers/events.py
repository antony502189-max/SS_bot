from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..db import get_session
from ..dependencies import current_user
from ..models import Event, Role, User
from ..schemas import EventCreate, EventOut
from ..services import create_event, require_role

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
