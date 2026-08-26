import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from apps.api.app.models import AuditLog, Base, Role, Sector, User, UserStatus
from apps.api.app.routers.admin import update_user
from apps.api.app.schemas import UserAdminUpdate
from apps.bot.app.admin_panel_v2 import admin_home_keyboard


@pytest.fixture
async def session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as value:
        yield value
    await engine.dispose()


def active_user(telegram_id: int, name: str, role: Role) -> User:
    return User(
        telegram_id=telegram_id,
        telegram_username=f"user{telegram_id}",
        full_name=name,
        normalized_full_name=name.casefold(),
        status=UserStatus.ACTIVE,
        role=role,
    )


def test_admin_home_contains_people_and_sectors() -> None:
    keyboard = admin_home_keyboard()
    texts = [button.text for row in keyboard.inline_keyboard for button in row]
    callbacks = [button.callback_data for row in keyboard.inline_keyboard for button in row]
    assert "👥 Пользователи" in texts
    assert "🏢 Сектора" in texts
    assert "admin:users" in callbacks
    assert "admin:sectors" in callbacks


@pytest.mark.asyncio
async def test_admin_can_assign_and_clear_user_sector_with_json_safe_audit(session) -> None:
    admin = active_user(5001, "Admin User", Role.ADMIN)
    participant = active_user(5002, "Sector User", Role.PARTICIPANT)
    sector = Sector(name="Operations", description="Operational sector")
    session.add_all([admin, participant, sector])
    await session.commit()
    await session.refresh(admin)
    await session.refresh(participant)
    await session.refresh(sector)

    updated = await update_user(
        participant.id,
        UserAdminUpdate(sector_id=sector.id),
        admin,
        session,
    )
    assert updated.sector_id == sector.id

    log = await session.scalar(
        select(AuditLog)
        .where(AuditLog.action == "admin.user_updated", AuditLog.entity_id == str(participant.id))
        .order_by(AuditLog.created_at.desc())
    )
    assert log is not None
    assert log.details["new"]["sector_id"] == str(sector.id)

    updated = await update_user(
        participant.id,
        UserAdminUpdate(sector_id=None),
        admin,
        session,
    )
    assert updated.sector_id is None


def test_uuid_callback_payloads_fit_telegram_limit() -> None:
    value = uuid.uuid4()
    assert len(f"admuser:{value}".encode()) <= 64
    assert len(f"usector:{value}".encode()) <= 64
    assert len(f"asetsec:{value}".encode()) <= 64
    assert len(f"sector:{value}".encode()) <= 64
    assert len(f"secrename:{value}".encode()) <= 64
    assert len(f"secdesc:{value}".encode()) <= 64
    assert len(f"secstate:{value}".encode()) <= 64
