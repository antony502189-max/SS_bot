import uuid

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..db import get_session
from ..models import Event, Role
from ..schemas import EventCreate, EventOut
from ..services import create_event, require_role

router = APIRouter(prefix="/events", tags=["events"])


@router.get("", response_model=list[EventOut])
async def list_events(session: AsyncSession = Depends(get_session)) -> list[Event]:
    return list((await session.scalars(select(Event).order_by(Event.starts_at.desc()))).all())


@router.post("", response_model=EventOut, status_code=201)
async def create_event_route(
    body: EventCreate,
    actor_id: uuid.UUID = Query(...),
    session: AsyncSession = Depends(get_session),
) -> Event:
    actor = await require_role(session, actor_id, Role.ADMIN, Role.SECTOR_HEAD)
    event = await create_event(session, actor, body)
    await session.commit()
    await session.refresh(event)
    return event
