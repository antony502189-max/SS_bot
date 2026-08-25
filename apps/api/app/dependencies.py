import uuid

from fastapi import Depends, Header
from sqlalchemy.ext.asyncio import AsyncSession

from .db import get_session
from .models import User
from .services import require_active_user


async def current_user(
    x_user_id: uuid.UUID = Header(alias="X-User-Id"), session: AsyncSession = Depends(get_session)
) -> User:
    return await require_active_user(session, x_user_id)
