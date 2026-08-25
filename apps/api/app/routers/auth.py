import json

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import get_settings
from ..db import get_session
from ..models import Role, User
from ..schemas import UserOut
from ..security import verify_mini_app_init_data

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/telegram", response_model=UserOut)
async def telegram_auth(init_data: str, session: AsyncSession = Depends(get_session)) -> User:
    data = verify_mini_app_init_data(init_data, get_settings().telegram_bot_token)
    try:
        telegram_user = json.loads(data["user"])
        telegram_id = int(telegram_user["id"])
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=401, detail="Telegram user data is invalid") from exc
    user = await session.scalar(select(User).where(User.telegram_id == telegram_id))
    if not user:
        role = Role.ADMIN if telegram_id in get_settings().bootstrap_admin_ids else Role.PARTICIPANT
        user = User(
            telegram_id=telegram_id, telegram_username=telegram_user.get("username"), role=role
        )
        session.add(user)
    else:
        user.telegram_username = telegram_user.get("username")
    await session.commit()
    await session.refresh(user)
    return user
