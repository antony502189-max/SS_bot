import uuid

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..db import get_session
from ..dependencies import current_user
from ..models import AuditLog, Role, Sector, User
from ..schemas import UserAdminUpdate, UserOut
from ..services import audit, require_role

router = APIRouter(prefix="/admin", tags=["admin"])


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
    audit_changes = body.model_dump(exclude_unset=True, mode="json")
    if "sector_id" in changes and changes["sector_id"]:
        sector = await session.get(Sector, changes["sector_id"])
        if not sector or not sector.is_active:
            raise HTTPException(status_code=422, detail="Active sector not found")

    target_role = changes.get("role", user.role)
    target_sector_id = changes.get("sector_id", user.sector_id)
    if target_role == Role.SECTOR_HEAD and target_sector_id is None:
        raise HTTPException(
            status_code=422,
            detail="Assign an active sector before making the user a sector head",
        )

    if user.id == actor.id and changes.get("status") and changes["status"].value != "active":
        raise HTTPException(status_code=422, detail="You cannot deactivate your own account")
    old = {
        key: getattr(user, key).value
        if hasattr(getattr(user, key), "value")
        else str(getattr(user, key))
        for key in changes
    }
    for key, value in changes.items():
        setattr(user, key, value)
    await audit(
        session,
        actor.id,
        "admin.user_updated",
        "user",
        user.id,
        {"old": old, "new": audit_changes},
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
