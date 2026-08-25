import uuid

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..db import get_session
from ..models import Role, Sector
from ..schemas import SectorCreate, SectorOut
from ..services import audit, require_role

router = APIRouter(prefix="/sectors", tags=["sectors"])


@router.get("", response_model=list[SectorOut])
async def list_sectors(session: AsyncSession = Depends(get_session)) -> list[Sector]:
    return list((await session.scalars(select(Sector).order_by(Sector.name))).all())


@router.post("", response_model=SectorOut, status_code=201)
async def create_sector(
    body: SectorCreate,
    actor_id: uuid.UUID = Query(...),
    session: AsyncSession = Depends(get_session),
) -> Sector:
    actor = await require_role(session, actor_id, Role.ADMIN)
    sector = Sector(name=body.name.strip(), description=body.description)
    session.add(sector)
    await session.flush()
    await audit(session, actor.id, "sector.created", "sector", sector.id)
    await session.commit()
    await session.refresh(sector)
    return sector
