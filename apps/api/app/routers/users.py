import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import get_settings
from ..db import get_session
from ..models import Role, User, UserStatus
from ..schemas import CompleteProfile, TelegramIdentity, UserOut
from ..services import audit, normalize_full_name, require_role

router = APIRouter(prefix="/users", tags=["users"])


@router.post("/telegram-sync", response_model=UserOut)
async def telegram_sync(
    identity: TelegramIdentity, session: AsyncSession = Depends(get_session)
) -> User:
    user = await session.scalar(select(User).where(User.telegram_id == identity.telegram_id))
    if not user:
        role = (
            Role.ADMIN
            if identity.telegram_id in get_settings().bootstrap_admin_ids
            else Role.PARTICIPANT
        )
        user = User(
            telegram_id=identity.telegram_id, telegram_username=identity.username, role=role
        )
        session.add(user)
    else:
        user.telegram_username = identity.username
    user.last_seen_at = datetime.now(UTC)
    await session.commit()
    await session.refresh(user)
    return user


@router.post("/{user_id}/profile", response_model=UserOut)
async def complete_profile(
    user_id: uuid.UUID, body: CompleteProfile, session: AsyncSession = Depends(get_session)
) -> User:
    user = await session.get(User, user_id)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    user.full_name = body.full_name.strip()
    user.normalized_full_name = normalize_full_name(body.full_name)
    user.status = UserStatus.ACTIVE
    await audit(session, user.id, "user.profile_completed", "user", user.id)
    await session.commit()
    await session.refresh(user)
    return user


@router.get("/me", response_model=UserOut)
async def me(user_id: uuid.UUID = Query(...), session: AsyncSession = Depends(get_session)) -> User:
    user = await session.get(User, user_id)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    return user


@router.get("/search", response_model=list[UserOut])
async def search_users(
    q: str = Query(min_length=2, max_length=100),
    actor_id: uuid.UUID = Query(...),
    session: AsyncSession = Depends(get_session),
) -> list[User]:
    await require_role(session, actor_id, Role.ADMIN, Role.SECTOR_HEAD)
    normalized = normalize_full_name(q)
    statement = (
        select(User)
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
    return list((await session.scalars(statement)).all())
