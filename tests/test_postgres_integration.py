import os
import uuid

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from apps.api.app.models import OutboxEvent, Role, Sector, User, UserStatus
from apps.api.app.routers.users import search_users
from apps.api.app.services import normalize_full_name

DATABASE_URL = os.getenv("DATABASE_URL", "")
pytestmark = pytest.mark.skipif(
    not DATABASE_URL.startswith("postgresql+asyncpg://"),
    reason="requires the PostgreSQL integration database",
)


@pytest.fixture
async def postgres_session():
    engine = create_async_engine(DATABASE_URL)
    connection = await engine.connect()
    transaction = await connection.begin()
    session = AsyncSession(bind=connection, expire_on_commit=False)
    try:
        yield session
    finally:
        await session.close()
        if transaction.is_active:
            await transaction.rollback()
        await connection.close()
        await engine.dispose()


@pytest.mark.asyncio
async def test_postgres_search_uses_trigram_schema_and_sector_scope(postgres_session) -> None:
    suffix = uuid.uuid4().hex[:10]
    first_sector = Sector(name=f"PG First {suffix}")
    second_sector = Sector(name=f"PG Second {suffix}")
    postgres_session.add_all([first_sector, second_sector])
    await postgres_session.flush()
    head = User(
        telegram_id=8_000_000_001,
        telegram_username=f"head_{suffix}",
        full_name="Руководитель Сектора",
        normalized_full_name=normalize_full_name("Руководитель Сектора"),
        status=UserStatus.ACTIVE,
        role=Role.SECTOR_HEAD,
        sector_id=first_sector.id,
    )
    visible = User(
        telegram_id=8_000_000_002,
        telegram_username=f"ivan_{suffix}",
        full_name="Иван Ёлкин",
        normalized_full_name=normalize_full_name("Иван Ёлкин"),
        status=UserStatus.ACTIVE,
        sector_id=first_sector.id,
    )
    other_sector = User(
        telegram_id=8_000_000_003,
        telegram_username=f"ivan_other_{suffix}",
        full_name="Иван Другой",
        normalized_full_name=normalize_full_name("Иван Другой"),
        status=UserStatus.ACTIVE,
        sector_id=second_sector.id,
    )
    inactive = User(
        telegram_id=8_000_000_004,
        telegram_username=f"inactive_{suffix}",
        full_name="Иван Неактивный",
        normalized_full_name=normalize_full_name("Иван Неактивный"),
        status=UserStatus.INACTIVE,
        sector_id=first_sector.id,
    )
    postgres_session.add_all([head, visible, other_sector, inactive])
    await postgres_session.flush()

    by_cyrillic_name = await search_users("елкин", head, postgres_session)
    by_username = await search_users(f"@IVAN_{suffix.upper()}", head, postgres_session)
    ambiguous = await search_users("иван", head, postgres_session)

    assert [item.id for item in by_cyrillic_name] == [visible.id]
    assert [item.id for item in by_username] == [visible.id]
    assert [item.id for item in ambiguous] == [visible.id]

    extension = await postgres_session.scalar(
        text("SELECT extname FROM pg_extension WHERE extname = 'pg_trgm'")
    )
    trigram_index = await postgres_session.scalar(
        text(
            "SELECT indexname FROM pg_indexes "
            "WHERE schemaname = 'public' AND indexname = 'ix_users_full_name_trgm' "
            "AND indexdef ILIKE '%gin_trgm_ops%'"
        )
    )
    assert extension == "pg_trgm"
    assert trigram_index == "ix_users_full_name_trgm"


@pytest.mark.asyncio
async def test_postgres_outbox_skip_locked_prevents_double_claim() -> None:
    engine = create_async_engine(DATABASE_URL)
    aggregate_id = f"locking-{uuid.uuid4()}"
    async with AsyncSession(engine, expire_on_commit=False) as seed:
        event = OutboxEvent(
            event_type="LOCK_TEST",
            aggregate_type="test",
            aggregate_id=aggregate_id,
            payload={},
        )
        seed.add(event)
        await seed.commit()
        event_id = event.id

    try:
        async with AsyncSession(engine, expire_on_commit=False) as first:
            async with first.begin():
                claimed = await first.scalar(
                    select(OutboxEvent)
                    .where(OutboxEvent.id == event_id)
                    .with_for_update(skip_locked=True)
                )
                assert claimed is not None
                async with AsyncSession(engine, expire_on_commit=False) as second:
                    async with second.begin():
                        duplicate = await second.scalar(
                            select(OutboxEvent)
                            .where(OutboxEvent.id == event_id)
                            .with_for_update(skip_locked=True)
                        )
                        assert duplicate is None
    finally:
        async with AsyncSession(engine) as cleanup:
            found = await cleanup.get(OutboxEvent, event_id)
            if found:
                await cleanup.delete(found)
                await cleanup.commit()
        await engine.dispose()
