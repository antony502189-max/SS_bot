import uuid
from datetime import UTC, datetime, timedelta
from io import BytesIO
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from telethon import errors, functions

from apps.api.app.archive import build_event_archive_pdf
from apps.api.app.main import app
from apps.api.app.models import (
    Base,
    Event,
    Notification,
    Role,
    Sector,
    Task,
    TaskChat,
    TaskKind,
    TaskMember,
    User,
    UserStatus,
)
from apps.api.app.photos import inspect_photo
from apps.api.app.schemas import EventCreate, TaskCreate
from apps.api.app.security import issue_access_token, verify_access_token
from apps.api.app.services import (
    create_event,
    create_task,
    normalize_full_name,
    refresh_event_retention,
    task_cleanup_at,
)
from apps.telegram_user_service.app.client import (
    TelegramResultKind,
    TelegramUserService,
    classify_error,
)


class FakeTelegramClient:
    def __init__(self) -> None:
        self.calls: list[object] = []
        self.pinned: tuple[object, object, bool] | None = None
        self.sent: tuple[object, str] | None = None

    async def get_input_entity(self, value: object) -> str:
        return f"entity:{value}"

    async def send_message(self, channel: object, text: str) -> SimpleNamespace:
        self.sent = (channel, text)
        return SimpleNamespace(id=42)

    async def pin_message(self, channel: object, message: object, *, notify: bool) -> None:
        self.pinned = (channel, message, notify)

    async def __call__(self, request: object) -> None:
        self.calls.append(request)


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
    assert normalize_full_name("  Ёлкин\u00a0Пётр ") == "елкин петр"


def test_signed_session_round_trip() -> None:
    token = issue_access_token("08a093e2-5b38-44be-9e6e-ae4e935c39fc", "test-secret", 5)
    assert verify_access_token(token, "test-secret") == "08a093e2-5b38-44be-9e6e-ae4e935c39fc"


def test_readiness_has_request_trace_id() -> None:
    client = TestClient(app)
    response = client.get("/readyz")
    assert response.status_code == 200
    assert response.headers["X-Request-ID"]


def test_cleanup_is_not_before_deadline() -> None:
    completed = datetime(2026, 8, 25, tzinfo=UTC)
    assert task_cleanup_at(completed, completed + timedelta(days=7)) == completed + timedelta(
        days=7
    )


def test_mtproto_membership_errors_have_actionable_states() -> None:
    already_member = classify_error(errors.UserAlreadyParticipantError(None))
    assert already_member.kind == TelegramResultKind.SUCCESS
    assert (
        classify_error(errors.UserNotParticipantError(None)).kind
        == TelegramResultKind.NOT_JOINED
    )


@pytest.mark.asyncio
async def test_mtproto_posts_and_pins_task_brief_without_network() -> None:
    adapter = object.__new__(TelegramUserService)
    fake = FakeTelegramClient()
    adapter.client = fake

    result = await adapter.post_and_pin_task_brief(
        777,
        "Set chairs",
        "Arrange the hall.",
        datetime(2026, 8, 26, 9, 0, tzinfo=UTC),
    )

    assert result.kind == TelegramResultKind.SUCCESS
    assert result.value == 42
    assert fake.sent == (
        "entity:777",
        "Task: Set chairs\nDeadline: 2026-08-26T09:00:00+00:00\n\nArrange the hall.",
    )
    assert fake.pinned is not None
    assert fake.pinned[2] is False


@pytest.mark.asyncio
async def test_mtproto_removes_member_with_ban_request_without_network() -> None:
    adapter = object.__new__(TelegramUserService)
    fake = FakeTelegramClient()
    adapter.client = fake

    result = await adapter.remove_user(777, "worker")

    assert result.kind == TelegramResultKind.SUCCESS
    assert len(fake.calls) == 1
    request = fake.calls[0]
    assert isinstance(request, functions.channels.EditBannedRequest)
    assert request.channel == "entity:777"
    assert request.participant == "entity:@worker"
    assert request.banned_rights.view_messages is True


def test_event_archive_pdf_contains_event_and_task_text() -> None:
    event = Event(
        id=uuid.uuid4(),
        title="Community day",
        starts_at=datetime(2026, 8, 25, tzinfo=UTC),
        created_by_id=uuid.uuid4(),
    )
    participant = User(telegram_id=9, full_name="Ada Lovelace")
    payload = build_event_archive_pdf(
        event,
        [participant],
        [
            {
                "title": "Set chairs",
                "status": "completed",
                "deadline": datetime(2026, 8, 26, tzinfo=UTC),
                "report": {"comment": "Done", "photo_count": 1},
            }
        ],
    )
    assert payload.startswith(b"%PDF")
    assert len(payload) > 1000


def test_photo_inspection_uses_real_image_format_and_bounded_preview() -> None:
    source = Image.new("RGB", (1800, 1200), color="navy")
    encoded = BytesIO()
    source.save(encoded, format="PNG")
    result = inspect_photo(encoded.getvalue())
    assert result.content_type == "image/png"
    assert (result.width, result.height) == (1800, 1200)
    with Image.open(BytesIO(result.preview_bytes)) as preview:
        assert preview.format == "JPEG"
        assert max(preview.size) <= 960


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
    notifications = list(
        (await session.scalars(select(Notification).where(Notification.task_id == task.id))).all()
    )
    assert [(notification.user_id, notification.type) for notification in notifications] == [
        (assignee.id, "TASK_ASSIGNED")
    ]


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


@pytest.mark.asyncio
async def test_group_task_creates_pending_chat_in_task_transaction(session) -> None:
    creator = User(
        telegram_id=10,
        full_name="Creator User",
        normalized_full_name="creator user",
        status=UserStatus.ACTIVE,
        role=Role.ADMIN,
    )
    leader = User(
        telegram_id=11,
        full_name="Leader User",
        normalized_full_name="leader user",
        status=UserStatus.ACTIVE,
    )
    session.add_all([creator, leader])
    await session.flush()
    cleanup_at = datetime.now(UTC) + timedelta(days=2)
    task, _ = await create_task(
        session,
        creator,
        TaskCreate(
            title="Group task",
            kind=TaskKind.GROUP,
            deadline=datetime.now(UTC) + timedelta(days=1),
            cleanup_at=cleanup_at,
            leader_id=leader.id,
            member_ids=[leader.id],
        ),
        "request-group-chat",
    )
    chat = await session.scalar(select(TaskChat).where(TaskChat.task_id == task.id))
    assert chat is not None
    assert chat.telegram_chat_id is None
    assert task.cleanup_at == cleanup_at


@pytest.mark.asyncio
async def test_sector_head_cannot_add_another_sector_participant_to_event(session) -> None:
    first_sector = Sector(name="First")
    second_sector = Sector(name="Second")
    session.add_all([first_sector, second_sector])
    await session.flush()
    head = User(
        telegram_id=3,
        full_name="Sector Head",
        normalized_full_name="sector head",
        status=UserStatus.ACTIVE,
        role=Role.SECTOR_HEAD,
        sector_id=first_sector.id,
    )
    outsider = User(
        telegram_id=4,
        full_name="Other Sector",
        normalized_full_name="other sector",
        status=UserStatus.ACTIVE,
        sector_id=second_sector.id,
    )
    session.add_all([head, outsider])
    await session.flush()
    with pytest.raises(HTTPException, match="only users in your sector"):
        await create_event(
            session,
            head,
            EventCreate(
                title="Event",
                starts_at=datetime.now(UTC) + timedelta(days=1),
                sector_id=first_sector.id,
                participant_ids=[outsider.id],
            ),
        )


@pytest.mark.asyncio
async def test_closed_event_task_sets_one_year_retention(session) -> None:
    creator = User(
        telegram_id=30,
        full_name="Admin User",
        normalized_full_name="admin user",
        status=UserStatus.ACTIVE,
        role=Role.ADMIN,
    )
    session.add(creator)
    await session.flush()
    event = Event(
        title="Event",
        starts_at=datetime.now(UTC),
        created_by_id=creator.id,
    )
    session.add(event)
    await session.flush()
    task = Task(
        event_id=event.id,
        title="Closed task",
        deadline=datetime.now(UTC),
        creator_id=creator.id,
        idempotency_key="retention-task",
        completed_at=datetime(2026, 8, 25, tzinfo=UTC),
    )
    session.add_all([event, task])
    await session.flush()
    await refresh_event_retention(session, event.id)
    assert event.retention_delete_at.date() == datetime(2027, 8, 25, tzinfo=UTC).date()
