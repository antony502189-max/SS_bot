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
    ChatStatus,
    Event,
    Notification,
    OutboxEvent,
    ReportStatus,
    Role,
    Sector,
    Task,
    TaskChat,
    TaskKind,
    TaskMember,
    TaskReport,
    TaskStatus,
    User,
    UserStatus,
)
from apps.api.app.photos import inspect_photo
from apps.api.app.routers import reports as report_routes
from apps.api.app.routers import tasks as task_routes
from apps.api.app.schemas import EventCreate, ReportCreate, ReportDecision, TaskCreate
from apps.api.app.security import issue_access_token, verify_access_token
from apps.api.app.services import (
    create_event,
    create_task,
    normalize_full_name,
    refresh_event_retention,
    task_cleanup_at,
)
from apps.bot.app.main import (
    is_exact_user_match,
    main_keyboard,
    parse_input_datetime,
    people_search_keyboard,
)
from apps.telegram_user_service.app.client import (
    TelegramResult,
    TelegramResultKind,
    TelegramUserService,
    classify_error,
    legacy_mtproto_channel_id,
    to_bot_api_chat_id,
)
from apps.worker.app import tasks as worker_tasks
from apps.worker.app.tasks import celery_app


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


def active_user(telegram_id: int, name: str, *, role: Role = Role.PARTICIPANT) -> User:
    return User(
        telegram_id=telegram_id,
        telegram_username=f"user{telegram_id}",
        full_name=name,
        normalized_full_name=normalize_full_name(name),
        status=UserStatus.ACTIVE,
        role=role,
    )


def test_full_name_normalization() -> None:
    assert normalize_full_name("  Ada   Lovelace ") == "ada lovelace"
    assert normalize_full_name("  Ёлкин\u00a0Пётр ") == "елкин петр"


def test_bot_dates_use_minsk_time() -> None:
    assert parse_input_datetime("31.12.2026 12:00") == datetime(2026, 12, 31, 9, tzinfo=UTC)


def test_people_search_keyboard_keeps_selection_without_a_done_button() -> None:
    first = active_user(101, "Анна Тест")
    second = active_user(102, "Борис Тест")
    first.id = uuid.uuid4()
    second.id = uuid.uuid4()

    keyboard = people_search_keyboard([first, second], {str(first.id), str(second.id)})

    assert len(keyboard.inline_keyboard) == 2
    assert keyboard.inline_keyboard[0][0].text.startswith("✅ Анна Тест")
    assert "@user101" in keyboard.inline_keyboard[0][0].text


def test_exact_user_match_accepts_full_name_or_username_only() -> None:
    user = active_user(101, "Анна Тест")

    assert is_exact_user_match(user, "Анна Тест")
    assert is_exact_user_match(user, "@user101")
    assert not is_exact_user_match(user, "Анна")


def test_admin_keyboard_contains_participant_directory_button() -> None:
    admin = active_user(1, "Администратор", role=Role.ADMIN)

    labels = [button.text for row in main_keyboard(admin).keyboard for button in row]

    assert "👥 База участников" in labels


def test_group_cleanup_runs_each_minute() -> None:
    assert celery_app.conf.beat_schedule["cleanup-every-minute"]["schedule"] == 60.0


def test_signed_session_round_trip() -> None:
    token = issue_access_token("08a093e2-5b38-44be-9e6e-ae4e935c39fc", "test-secret", 5)
    assert verify_access_token(token, "test-secret") == "08a093e2-5b38-44be-9e6e-ae4e935c39fc"


def test_readiness_has_request_trace_id(monkeypatch) -> None:
    import apps.api.app.main as api_main

    test_engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    monkeypatch.setattr(api_main, "engine", test_engine)
    with TestClient(app) as client:
        response = client.get("/readyz")
    assert response.status_code == 200
    assert response.headers["X-Request-ID"]


def test_cleanup_is_three_days_after_later_of_deadline_and_closure() -> None:
    completed_before = datetime(2026, 8, 25, tzinfo=UTC)
    deadline = datetime(2026, 9, 1, tzinfo=UTC)
    assert task_cleanup_at(completed_before, deadline) == deadline + timedelta(days=3)

    completed_after = datetime(2026, 9, 5, tzinfo=UTC)
    assert task_cleanup_at(completed_after, deadline) == completed_after + timedelta(days=3)


def test_telegram_chat_id_round_trip() -> None:
    assert to_bot_api_chat_id(1234567890) == -1001234567890
    assert to_bot_api_chat_id(-1001234567890) == -1001234567890
    assert legacy_mtproto_channel_id(-1001234567890) == 1234567890


def test_mtproto_membership_errors_have_actionable_states() -> None:
    already_member = classify_error(errors.UserAlreadyParticipantError(None))
    assert already_member.kind == TelegramResultKind.SUCCESS
    assert classify_error(errors.UserNotParticipantError(None)).kind == TelegramResultKind.NOT_JOINED


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
        "Задача: Set chairs\nСрок: 2026-08-26T09:00:00+00:00\n\nArrange the hall.",
    )
    assert fake.pinned is not None
    assert fake.pinned[2] is False


@pytest.mark.asyncio
async def test_mtproto_removes_member_and_clears_ban_without_network() -> None:
    adapter = object.__new__(TelegramUserService)
    fake = FakeTelegramClient()
    adapter.client = fake

    result = await adapter.remove_user(777, "worker")

    assert result.kind == TelegramResultKind.SUCCESS
    assert len(fake.calls) == 2
    assert all(isinstance(request, functions.channels.EditBannedRequest) for request in fake.calls)
    assert fake.calls[0].banned_rights.view_messages is True
    assert fake.calls[1].banned_rights.view_messages is False


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
    creator = active_user(1, "Admin User", role=Role.ADMIN)
    assignee = active_user(2, "Worker User")
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
async def test_admin_can_permanently_delete_an_accidental_task(session) -> None:
    admin = active_user(1, "Admin User", role=Role.ADMIN)
    assignee = active_user(2, "Worker User")
    session.add_all([admin, assignee])
    await session.flush()
    task, _ = await create_task(
        session,
        admin,
        TaskCreate(
            title="Accidental task",
            deadline=datetime.now(UTC) + timedelta(days=1),
            member_ids=[assignee.id],
        ),
        "delete-accidental-task",
    )
    task_id = task.id

    await task_routes.delete_task(task_id, admin, session)

    assert await session.get(Task, task_id) is None
    notices = list((await session.scalars(select(Notification))).all())
    assert [(notice.user_id, notice.type, notice.task_id) for notice in notices] == [
        (assignee.id, "TASK_DELETED", None)
    ]


@pytest.mark.asyncio
async def test_permanent_task_delete_rejects_existing_working_group(session) -> None:
    admin = active_user(10, "Admin User", role=Role.ADMIN)
    assignee = active_user(11, "Worker User")
    session.add_all([admin, assignee])
    await session.flush()
    task, _ = await create_task(
        session,
        admin,
        TaskCreate(
            title="Task with group",
            kind=TaskKind.GROUP,
            deadline=datetime.now(UTC) + timedelta(days=1),
            leader_id=assignee.id,
            member_ids=[assignee.id],
        ),
        "delete-task-with-group",
    )
    chat = await session.scalar(select(TaskChat).where(TaskChat.task_id == task.id))
    assert chat is not None
    chat.telegram_chat_id = -1001234567890
    await session.flush()

    with pytest.raises(HTTPException, match="рабочая группа"):
        await task_routes.delete_task(task.id, admin, session)


@pytest.mark.asyncio
async def test_group_task_needs_selected_leader(session) -> None:
    creator = active_user(1, "Admin User", role=Role.ADMIN)
    leader = active_user(2, "Leader User")
    session.add_all([creator, leader])
    await session.flush()
    with pytest.raises(HTTPException, match="requires a leader"):
        await create_task(
            session,
            creator,
            TaskCreate(
                title="Group task",
                kind="group",
                deadline=datetime.now(UTC) + timedelta(days=1),
                member_ids=[leader.id],
            ),
            "request-2",
        )
    with pytest.raises(HTTPException, match="selected assignee"):
        await create_task(
            session,
            creator,
            TaskCreate(
                title="Group task",
                kind="group",
                deadline=datetime.now(UTC) + timedelta(days=1),
                leader_id=leader.id,
                member_ids=[],
            ),
            "request-3",
        )


@pytest.mark.asyncio
async def test_group_task_creates_pending_chat_without_manual_cleanup(session) -> None:
    creator = active_user(10, "Creator User", role=Role.ADMIN)
    leader = active_user(11, "Leader User")
    session.add_all([creator, leader])
    await session.flush()
    task, _ = await create_task(
        session,
        creator,
        TaskCreate(
            title="Group task",
            kind=TaskKind.GROUP,
            deadline=datetime.now(UTC) + timedelta(days=1),
            leader_id=leader.id,
            member_ids=[leader.id],
        ),
        "request-group-chat",
    )
    chat = await session.scalar(select(TaskChat).where(TaskChat.task_id == task.id))
    assert chat is not None
    assert chat.telegram_chat_id is None
    assert task.cleanup_at is None


@pytest.mark.asyncio
async def test_sector_head_implicitly_uses_own_sector(session) -> None:
    sector = Sector(name="First")
    session.add(sector)
    await session.flush()
    head = active_user(20, "Sector Head", role=Role.SECTOR_HEAD)
    head.sector_id = sector.id
    worker = active_user(21, "Worker")
    worker.sector_id = sector.id
    session.add_all([head, worker])
    await session.flush()

    task, _ = await create_task(
        session,
        head,
        TaskCreate(
            title="Sector task",
            deadline=datetime.now(UTC) + timedelta(days=1),
            member_ids=[worker.id],
        ),
        "sector-task",
    )
    assert task.sector_id == sector.id


@pytest.mark.asyncio
async def test_idempotency_key_cannot_leak_another_creators_task(session) -> None:
    first = active_user(30, "First Admin", role=Role.ADMIN)
    second = active_user(31, "Second Admin", role=Role.ADMIN)
    worker = active_user(32, "Worker")
    session.add_all([first, second, worker])
    await session.flush()
    await create_task(
        session,
        first,
        TaskCreate(
            title="Private task",
            deadline=datetime.now(UTC) + timedelta(days=1),
            member_ids=[worker.id],
        ),
        "shared-key",
    )
    await session.flush()
    with pytest.raises(HTTPException, match="already in use"):
        await create_task(
            session,
            second,
            TaskCreate(
                title="Other task",
                deadline=datetime.now(UTC) + timedelta(days=1),
                member_ids=[worker.id],
            ),
            "shared-key",
        )


@pytest.mark.asyncio
async def test_report_can_be_returned_resubmitted_and_approved(session) -> None:
    creator = active_user(40, "Creator", role=Role.ADMIN)
    leader = active_user(41, "Leader")
    worker = active_user(42, "Worker")
    session.add_all([creator, leader, worker])
    await session.flush()
    task, _ = await create_task(
        session,
        creator,
        TaskCreate(
            title="Group report",
            kind=TaskKind.GROUP,
            deadline=datetime.now(UTC) + timedelta(days=1),
            leader_id=leader.id,
            member_ids=[leader.id, worker.id],
        ),
        "report-cycle",
    )
    await session.commit()

    first = await report_routes.submit_report(
        task.id, ReportCreate(comment="First version"), worker, session
    )
    assert first.status == ReportStatus.SUBMITTED
    assert task.status == TaskStatus.SUBMITTED

    returned = await report_routes.decide_report(
        task.id,
        ReportDecision(approved=False, reason="Fix details"),
        leader,
        session,
    )
    assert returned.status == ReportStatus.RETURNED
    assert task.status == TaskStatus.RETURNED

    second = await report_routes.submit_report(
        task.id, ReportCreate(comment="Fixed version"), worker, session
    )
    assert second.id == first.id
    assert second.status == ReportStatus.SUBMITTED
    assert second.comment == "Fixed version"

    approved = await report_routes.decide_report(
        task.id,
        ReportDecision(approved=True),
        leader,
        session,
    )
    assert approved.status == ReportStatus.APPROVED
    assert task.status == TaskStatus.COMPLETED
    assert task.cleanup_at == max(task.completed_at, task.deadline) + timedelta(days=3)


@pytest.mark.asyncio
async def test_individual_report_completes_only_on_submit(session) -> None:
    creator = active_user(50, "Creator", role=Role.ADMIN)
    worker = active_user(51, "Worker")
    session.add_all([creator, worker])
    await session.flush()
    task, _ = await create_task(
        session,
        creator,
        TaskCreate(
            title="Individual report",
            deadline=datetime.now(UTC) + timedelta(days=1),
            member_ids=[worker.id],
        ),
        "individual-report",
    )
    draft = TaskReport(
        task_id=task.id,
        submitted_by_id=worker.id,
        status=ReportStatus.DRAFT,
    )
    session.add(draft)
    await session.commit()
    assert task.status == TaskStatus.ACTIVE
    assert task.cleanup_at is None

    result = await report_routes.submit_report(
        task.id, ReportCreate(comment="Done"), worker, session
    )
    assert result.status == ReportStatus.APPROVED
    assert task.status == TaskStatus.COMPLETED
    assert task.cleanup_at is not None


@pytest.mark.asyncio
async def test_cleanup_never_deletes_active_task_even_if_bad_legacy_timestamp(session, monkeypatch) -> None:
    creator = active_user(60, "Cleanup Creator", role=Role.ADMIN)
    session.add(creator)
    await session.flush()
    task = Task(
        title="Still active",
        kind=TaskKind.GROUP,
        status=TaskStatus.ACTIVE,
        deadline=datetime.now(UTC) + timedelta(days=1),
        cleanup_at=datetime.now(UTC) - timedelta(minutes=1),
        creator_id=creator.id,
        idempotency_key="cleanup-active",
    )
    session.add(task)
    await session.flush()
    chat = TaskChat(task_id=task.id, telegram_chat_id=-1001234567890, status=ChatStatus.READY)
    session.add(chat)
    await session.commit()

    deleted_chat_ids: list[int] = []

    class FakeTelegramService:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, traceback):
            return False

        async def delete_supergroup(self, chat_id: int) -> TelegramResult:
            deleted_chat_ids.append(chat_id)
            return TelegramResult(TelegramResultKind.SUCCESS)

    class ExistingSession:
        async def __aenter__(self):
            return session

        async def __aexit__(self, exc_type, exc, traceback):
            return False

    monkeypatch.setattr(worker_tasks, "SessionLocal", lambda: ExistingSession())
    monkeypatch.setattr(worker_tasks, "TelegramUserService", FakeTelegramService)
    await worker_tasks._process_cleanup()

    assert deleted_chat_ids == []
    assert chat.status == ChatStatus.READY


@pytest.mark.asyncio
async def test_cleanup_deletes_closed_group_when_scheduled_time_arrives(session, monkeypatch) -> None:
    creator = active_user(70, "Cleanup Creator", role=Role.ADMIN)
    session.add(creator)
    await session.flush()
    task = Task(
        title="Cleanup group",
        kind=TaskKind.GROUP,
        status=TaskStatus.COMPLETED,
        deadline=datetime.now(UTC) - timedelta(days=4),
        completed_at=datetime.now(UTC) - timedelta(days=4),
        cleanup_at=datetime.now(UTC) - timedelta(minutes=1),
        creator_id=creator.id,
        idempotency_key="cleanup-group",
    )
    session.add(task)
    await session.flush()
    chat = TaskChat(task_id=task.id, telegram_chat_id=-1001234567890, status=ChatStatus.READY)
    session.add(chat)
    await session.commit()

    deleted_chat_ids: list[int] = []

    class FakeTelegramService:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, traceback):
            return False

        async def revoke_invite(self, chat_id: int, link: str) -> TelegramResult:
            return TelegramResult(TelegramResultKind.SUCCESS)

        async def delete_supergroup(self, chat_id: int) -> TelegramResult:
            deleted_chat_ids.append(chat_id)
            return TelegramResult(TelegramResultKind.SUCCESS)

    class ExistingSession:
        async def __aenter__(self):
            return session

        async def __aexit__(self, exc_type, exc, traceback):
            return False

    async def archive_is_ready(*_args) -> bool:
        return True

    monkeypatch.setattr(worker_tasks, "SessionLocal", lambda: ExistingSession())
    monkeypatch.setattr(worker_tasks, "TelegramUserService", FakeTelegramService)
    monkeypatch.setattr(worker_tasks, "archive_is_persisted", archive_is_ready)

    await worker_tasks._process_cleanup()

    await session.refresh(chat)
    assert deleted_chat_ids == [-1001234567890]
    assert chat.status == ChatStatus.DELETED


@pytest.mark.asyncio
async def test_outbox_ignores_future_events(session, monkeypatch) -> None:
    event = OutboxEvent(
        event_type="TASK_CREATED",
        aggregate_type="task",
        aggregate_id="x",
        payload={"task_id": str(uuid.uuid4())},
        available_at=datetime.now(UTC) + timedelta(hours=1),
    )
    session.add(event)
    await session.commit()

    class ExistingSession:
        async def __aenter__(self):
            return session

        async def __aexit__(self, exc_type, exc, traceback):
            return False

    called = False

    async def should_not_run(*_args):
        nonlocal called
        called = True

    monkeypatch.setattr(worker_tasks, "SessionLocal", lambda: ExistingSession())
    monkeypatch.setattr(worker_tasks, "provision_task_chat", should_not_run)
    await worker_tasks._process_outbox()

    assert called is False
    await session.refresh(event)
    assert event.processed_at is None


@pytest.mark.asyncio
async def test_sector_head_cannot_add_another_sector_participant_to_event(session) -> None:
    first_sector = Sector(name="First")
    second_sector = Sector(name="Second")
    session.add_all([first_sector, second_sector])
    await session.flush()
    head = active_user(80, "Sector Head", role=Role.SECTOR_HEAD)
    head.sector_id = first_sector.id
    outsider = active_user(81, "Other Sector")
    outsider.sector_id = second_sector.id
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
    creator = active_user(90, "Admin User", role=Role.ADMIN)
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
        status=TaskStatus.COMPLETED,
    )
    session.add(task)
    await session.flush()
    await refresh_event_retention(session, event.id)
    assert event.retention_delete_at.date() == datetime(2027, 8, 25, tzinfo=UTC).date()
