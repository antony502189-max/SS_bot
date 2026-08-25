from datetime import UTC, datetime, timedelta

import pytest
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from apps.api.app.models import Base, Role, TaskMember, User, UserStatus
from apps.api.app.schemas import TaskCreate
from apps.api.app.services import create_task, normalize_full_name, task_cleanup_at


@pytest.fixture
async def session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as value:
        yield value
    await engine.dispose()


def test_full_name_normalization() -> None:
    assert normalize_full_name("  Ada   Lovelace ") == "ada lovelace"


def test_cleanup_is_not_before_deadline() -> None:
    completed = datetime(2026, 8, 25, tzinfo=UTC)
    assert task_cleanup_at(completed, completed + timedelta(days=7)) == completed + timedelta(
        days=7
    )


@pytest.mark.asyncio
async def test_creator_is_auto_added_once(session) -> None:
    creator = User(
        telegram_id=1,
        full_name="Admin User",
        normalized_full_name="admin user",
        status=UserStatus.ACTIVE,
        role=Role.ADMIN,
    )
    assignee = User(
        telegram_id=2,
        full_name="Worker User",
        normalized_full_name="worker user",
        status=UserStatus.ACTIVE,
    )
    session.add_all([creator, assignee])
    await session.flush()
    task, reused = await create_task(
        session,
        creator,
        TaskCreate(
            title="Set chairs",
            deadline=datetime.now(UTC) + timedelta(days=1),
            member_ids=[creator.id, assignee.id],
        ),
        "request-1",
    )
    assert reused is False
    members = list(
        (await session.scalars(select(TaskMember).where(TaskMember.task_id == task.id))).all()
    )
    assert {member.user_id for member in members} == {creator.id, assignee.id}
    assert sum(member.is_creator for member in members) == 1


@pytest.mark.asyncio
async def test_group_task_needs_leader(session) -> None:
    creator = User(
        telegram_id=1,
        full_name="Admin User",
        normalized_full_name="admin user",
        status=UserStatus.ACTIVE,
        role=Role.ADMIN,
    )
    session.add(creator)
    await session.flush()
    with pytest.raises(HTTPException, match="requires a leader"):
        await create_task(
            session,
            creator,
            TaskCreate(
                title="Group task", kind="group", deadline=datetime.now(UTC) + timedelta(days=1)
            ),
            "request-2",
        )
