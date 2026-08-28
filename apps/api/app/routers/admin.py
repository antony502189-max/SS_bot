import uuid

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..db import get_session
from ..dependencies import current_user
from ..models import AuditLog, Role, Sector, User, UserStatus
from ..schemas import UserAdminUpdate, UserOut
from ..services import audit, require_role

router = APIRouter(prefix="/admin", tags=["admin"])


def _audit_value(value: object) -> object:
    if hasattr(value, "value"):
        return value.value
    if isinstance(value, uuid.UUID):
        return str(value)
    return value


async def admin_actor(
    actor: User = Depends(current_user), session: AsyncSession = Depends(get_session)
) -> User:
    return await require_role(session, actor.id, Role.ADMIN)


@router.get("/users", response_model=list[UserOut])
async def list_users(
    _: User = Depends(admin_actor),
    session: AsyncSession = Depends(get_session),
    limit: int = Query(default=100, ge=1, le=250),
) -> list[User]:
    return list((await session.scalars(select(User).order_by(User.full_name).limit(limit))).all())


@router.patch("/users/{user_id}", response_model=UserOut)
async def update_user(
    user_id: uuid.UUID,
    body: UserAdminUpdate,
    actor: User = Depends(admin_actor),
    session: AsyncSession = Depends(get_session),
) -> User:
    user = await session.get(User, user_id)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    changes = body.model_dump(exclude_unset=True)
    if "sector_id" in changes and changes["sector_id"]:
        sector = await session.get(Sector, changes["sector_id"])
        if not sector or not sector.is_active:
            raise HTTPException(status_code=422, detail="An active sector is required")
    resulting_role = changes.get("role", user.role)
    resulting_sector_id = changes.get("sector_id", user.sector_id)
    if resulting_role == Role.SECTOR_HEAD:
        if not resulting_sector_id:
            raise HTTPException(status_code=422, detail="A sector head must have an active sector")
        sector = await session.get(Sector, resulting_sector_id)
        if not sector or not sector.is_active:
            raise HTTPException(status_code=422, detail="A sector head must have an active sector")
    if changes.get("status") == UserStatus.INACTIVE and user.id == actor.id:
        raise HTTPException(status_code=422, detail="You cannot deactivate your own account")
    if user.id == actor.id and changes.get("status") and changes["status"].value != "active":
        raise HTTPException(status_code=422, detail="You cannot deactivate your own account")
    old = {key: _audit_value(getattr(user, key)) for key in changes}
    serialized_changes = {key: _audit_value(value) for key, value in changes.items()}
    for key, value in changes.items():
        setattr(user, key, value)
    await audit(
        session,
        actor.id,
        "admin.user_updated",
        "user",
        user.id,
        {"old": old, "new": serialized_changes},
    )
    await session.commit()
    await session.refresh(user)
    return user


@router.get("/audit")
async def list_audit(
    _: User = Depends(admin_actor),
    session: AsyncSession = Depends(get_session),
    limit: int = Query(default=100, ge=1, le=250),
) -> list[dict]:
    logs = list(
        (
            await session.scalars(
                select(AuditLog).order_by(AuditLog.created_at.desc()).limit(limit)
            )
        ).all()
    )
    return [
        {
            "id": str(log.id),
            "action": log.action,
            "entity_type": log.entity_type,
            "entity_id": log.entity_id,
            "details": log.details,
            "created_at": log.created_at,
        }
        for log in logs
    ]
