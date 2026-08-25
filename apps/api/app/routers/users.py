import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..db import get_session
from ..dependencies import current_user
from ..models import Role, Sector, User, UserStatus
from ..schemas import CompleteProfile, UserOut, UserSearchResult
from ..services import audit, normalize_full_name, require_role

router = APIRouter(tags=["users"])


@router.get("/me", response_model=UserOut)
async def me(actor: User = Depends(current_user)) -> User:
    return actor


@router.patch("/me/profile", response_model=UserOut)
async def complete_profile(
    body: CompleteProfile,
    actor: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> User:
    actor.full_name = body.full_name.strip()
    actor.normalized_full_name = normalize_full_name(body.full_name)
    actor.status = UserStatus.ACTIVE if actor.telegram_username else UserStatus.NEEDS_USERNAME
    await audit(session, actor.id, "user.profile_completed", "user", actor.id)
    await session.commit()
    await session.refresh(actor)
    return actor


@router.get("/users/search", response_model=list[UserSearchResult])
async def search_users(
    q: str = Query(min_length=2, max_length=100),
    actor: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> list[UserSearchResult]:
    await require_role(session, actor.id, Role.ADMIN, Role.SECTOR_HEAD)
    normalized = normalize_full_name(q)
    statement = (
        select(User, Sector.name)
        .outerjoin(Sector, Sector.id == User.sector_id)
        .where(
            User.status == UserStatus.ACTIVE,
            or_(
                User.normalized_full_name.contains(normalized),
                User.telegram_username.ilike(f"%{q.lstrip('@')}%"),
            ),
        )
        .order_by(User.full_name)
        .limit(20)
    )
    return [
        UserSearchResult.model_validate(user, from_attributes=True).model_copy(
            update={"sector_name": sector_name}
        )
        for user, sector_name in (await session.execute(statement)).all()
    ]


@router.post("/me/refresh-telegram", response_model=UserOut)
async def refresh_telegram_identity(
    actor: User = Depends(current_user), session: AsyncSession = Depends(get_session)
) -> User:
    actor.last_seen_at = datetime.now(UTC)
    await session.commit()
    await session.refresh(actor)
    return actor


@router.get("/users/{user_id}", response_model=UserOut)
async def get_user(
    user_id: uuid.UUID,
    actor: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> User:
    await require_role(session, actor.id, Role.ADMIN, Role.SECTOR_HEAD)
    user = await session.get(User, user_id)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    if actor.role == Role.SECTOR_HEAD and user.sector_id != actor.sector_id:
        raise HTTPException(status_code=403, detail="You can view only your sector")
    return user
