import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..db import get_session
from ..dependencies import current_user
from ..models import Role, Sector, User
from ..schemas import SectorCreate, SectorOut, SectorUpdate
from ..services import audit, require_role

router = APIRouter(prefix="/sectors", tags=["sectors"])


@router.get("", response_model=list[SectorOut])
async def list_sectors(
    _: User = Depends(current_user), session: AsyncSession = Depends(get_session)
) -> list[Sector]:
    return list(
        (await session.scalars(select(Sector).where(Sector.is_active).order_by(Sector.name))).all()
    )


@router.post("", response_model=SectorOut, status_code=201)
async def create_sector(
    body: SectorCreate,
    actor: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> Sector:
    await require_role(session, actor.id, Role.ADMIN)
    sector = Sector(name=body.name.strip(), description=body.description)
    session.add(sector)
    await session.flush()
    await audit(session, actor.id, "sector.created", "sector", sector.id)
    await session.commit()
    await session.refresh(sector)
    return sector


@router.patch("/{sector_id}", response_model=SectorOut)
async def update_sector(
    sector_id: uuid.UUID,
    body: SectorUpdate,
    actor: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> Sector:
    await require_role(session, actor.id, Role.ADMIN)
    sector = await session.get(Sector, sector_id)
    if not sector:
        raise HTTPException(status_code=404, detail="Sector not found")
    updates = body.model_dump(exclude_unset=True)
    for key, value in updates.items():
        setattr(sector, key, value.strip() if key == "name" and value else value)
    await audit(session, actor.id, "sector.updated", "sector", sector.id, updates)
    await session.commit()
    await session.refresh(sector)
    return sector


@router.delete("/{sector_id}", status_code=204)
async def deactivate_sector(
    sector_id: uuid.UUID,
    actor: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> None:
    await require_role(session, actor.id, Role.ADMIN)
    sector = await session.get(Sector, sector_id)
    if not sector:
        raise HTTPException(status_code=404, detail="Sector not found")
    sector.is_active = False
    await audit(session, actor.id, "sector.deactivated", "sector", sector.id)
    await session.commit()
