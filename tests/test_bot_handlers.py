import ast
import uuid
import zipfile
from datetime import UTC, datetime, timedelta
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select

from apps.api.app import archive as archive_module
from apps.api.app.models import (
    AuditLog,
    Event,
    EventParticipant,
    OutboxEvent,
    ReportStatus,
    Role,
    Sector,
    Task,
    TaskChat,
    TaskChecklistItem,
    TaskKind,
    TaskMember,
    TaskPhoto,
    TaskReport,
    TaskStatus,
    User,
    UserStatus,
)
from apps.api.app.services import normalize_full_name
from apps.bot.app.handlers import core as bot


class FakeState:
    def __init__(self, **data):
        self.data = data
        self.state = None

    async def get_data(self):
        return dict(self.data)

    async def update_data(self, **values):
        self.data.update(values)

    async def set_state(self, value):
        self.state = value

    async def clear(self):
        self.data.clear()
        self.state = None


class FakeMessage:
    def __init__(self, text: str = "", reply_markup=None, from_user=None):
        self.text = text
        self.reply_markup = reply_markup
        self.from_user = from_user
        self.answers: list[tuple[str, object]] = []
        self.edits: list[tuple[str, object]] = []
        self.documents: list[object] = []

    async def answer(self, text, reply_markup=None):
        self.answers.append((text, reply_markup))

    async def edit_text(self, text, reply_markup=None):
        self.edits.append((text, reply_markup))
        self.reply_markup = reply_markup

    async def edit_reply_markup(self, reply_markup=None):
        self.reply_markup = reply_markup

    async def answer_document(self, document):
        self.documents.append(document)


class FakeCallback:
    def __init__(self, data: str, message: FakeMessage, from_user=None):
        self.data = data
        self.message = message
        self.from_user = from_user
        self.answers: list[tuple[object, bool]] = []

    async def answer(self, text=None, show_alert=False):
        self.answers.append((text, show_alert))


class ExistingSession:
    def __init__(self, session):
        self.session = session

    async def __aenter__(self):
        return self.session

    async def __aexit__(self, exc_type, exc, traceback):
        return False


def use_existing_session(monkeypatch, session) -> None:
    monkeypatch.setattr(bot, "SessionLocal", lambda: ExistingSession(session))


def user(number: int, name: str, *, role=Role.PARTICIPANT, sector_id=None, active=True):
    value = User(
        id=uuid.uuid4(),
        telegram_id=number,
        telegram_username=f"user{number}",
        full_name=name,
        normalized_full_name=normalize_full_name(name),
        role=role,
        status=UserStatus.ACTIVE if active else UserStatus.INACTIVE,
        sector_id=sector_id,
    )
    return value


@pytest.mark.asyncio
async def test_registration_start_is_idempotent_and_persists_full_name(session, monkeypatch) -> None:
    use_existing_session(monkeypatch, session)
    monkeypatch.setattr(bot, "get_settings", lambda: SimpleNamespace(bootstrap_admin_ids=set()))
    telegram_user = SimpleNamespace(id=1001, username="new_user")
    state = FakeState()

    await bot.start(FakeMessage("/start", from_user=telegram_user), state)
    assert state.state == bot.Registration.full_name
    assert await session.scalar(select(func.count()).select_from(User)) == 1

    await bot.start(FakeMessage("/start", from_user=telegram_user), state)
    assert await session.scalar(select(func.count()).select_from(User)) == 1

    invalid = FakeMessage("Single", from_user=telegram_user)
    await bot.receive_full_name(invalid, state)
    assert "имя и фамилию" in invalid.answers[-1][0]
    assert state.state == bot.Registration.full_name

    valid = FakeMessage("  Ada   Lovelace  ", from_user=telegram_user)
    await bot.receive_full_name(valid, state)
    registered = await session.scalar(select(User).where(User.telegram_id == telegram_user.id))
    assert registered is not None
    assert registered.full_name == "Ada   Lovelace"
    assert registered.normalized_full_name == "ada lovelace"
    assert registered.status == UserStatus.ACTIVE
    assert state.state is None
    assert valid.answers[-1][0] == "Регистрация завершена."

    ready = FakeMessage("/start", from_user=telegram_user)
    await bot.start(ready, state)
    assert ready.answers[-1][0] == "SS Bot готов к работе."
    assert await session.scalar(select(func.count()).select_from(User)) == 1


@pytest.mark.asyncio
async def test_registration_synchronizes_username_and_preserves_disabled_status(
    session, monkeypatch
) -> None:
    use_existing_session(monkeypatch, session)
    monkeypatch.setattr(bot, "get_settings", lambda: SimpleNamespace(bootstrap_admin_ids=set()))
    active = user(1010, "Active User")
    blocked = user(1011, "Blocked User")
    blocked.status = UserStatus.BLOCKED
    session.add_all([active, blocked])
    await session.commit()

    synced = await bot.sync_telegram_user(SimpleNamespace(id=active.telegram_id, username="renamed"))
    assert synced.telegram_username == "renamed"
    assert synced.status == UserStatus.ACTIVE

    synced = await bot.sync_telegram_user(SimpleNamespace(id=active.telegram_id, username=None))
    assert synced.telegram_username is None
    assert synced.status == UserStatus.NEEDS_USERNAME

    state = FakeState()
    blocked_message = FakeMessage(
        "/start",
        from_user=SimpleNamespace(id=blocked.telegram_id, username=blocked.telegram_username),
    )
    await bot.start(blocked_message, state)
    assert "профиль отключён" in blocked_message.answers[-1][0]
    assert blocked.status == UserStatus.BLOCKED

    pending = User(
        telegram_id=1012,
        telegram_username="pending_user",
        status=UserStatus.PENDING_PROFILE,
    )
    session.add(pending)
    await session.commit()
    pending_state = FakeState()
    pending_message = FakeMessage(
        "/start", from_user=SimpleNamespace(id=pending.telegram_id, username="pending_user")
    )
    await bot.start(pending_message, pending_state)
    pending.status = UserStatus.INACTIVE
    await session.commit()
    finish = FakeMessage(
        "Pending User", from_user=SimpleNamespace(id=pending.telegram_id, username="pending_user")
    )
    await bot.receive_full_name(finish, pending_state)
    await session.refresh(pending)
    assert pending.status == UserStatus.INACTIVE
    assert pending.full_name is None
    assert pending_state.state is None
    assert "профиль отключён" in finish.answers[-1][0]


def test_role_aware_main_menu_has_only_authorized_actions() -> None:
    participant_labels = {
        button.text for row in bot.main_keyboard(user(1020, "Participant")).keyboard for button in row
    }
    head_labels = {
        button.text
        for row in bot.main_keyboard(user(1021, "Head", role=Role.SECTOR_HEAD)).keyboard
        for button in row
    }
    admin_labels = {
        button.text
        for row in bot.main_keyboard(user(1022, "Admin", role=Role.ADMIN)).keyboard
        for button in row
    }

    assert "➕ Выдать задачу" not in participant_labels
    assert "⚙️ Администрирование" not in participant_labels
    assert "➕ Выдать задачу" in head_labels
    assert "⚙️ Администрирование" not in head_labels
    assert {"➕ Выдать задачу", "⚙️ Администрирование", "👥 База участников"} <= admin_labels


@pytest.mark.asyncio
async def test_admin_directory_card_role_and_status_handlers(session, monkeypatch) -> None:
    admin = user(1030, "Admin", role=Role.ADMIN)
    sector = Sector(name="Operations")
    session.add_all([admin, sector])
    await session.flush()
    targets = [user(1031 + index, f"Person {index:02d}", sector_id=sector.id) for index in range(16)]
    session.add_all(targets)
    await session.commit()
    use_existing_session(monkeypatch, session)

    async def sync_admin(_update):
        return admin

    monkeypatch.setattr(bot, "sync_user", sync_admin)
    monkeypatch.setattr(bot, "sync_callback_user", sync_admin)

    directory_message = FakeMessage()
    await bot.user_directory(directory_message)
    text, keyboard = directory_message.answers[-1]
    assert "Страница 1 из 2" in text
    assert keyboard.inline_keyboard[-1][0].callback_data == "users:page:1"

    page_message = FakeMessage()
    await bot.user_directory_page(FakeCallback("users:page:1", page_message))
    assert "Страница 2 из 2" in page_message.edits[-1][0]

    target = targets[0]
    card_message = FakeMessage()
    await bot.admin_user_detail(FakeCallback(f"admuser:{target.id}:1", card_message))
    assert target.full_name in card_message.edits[-1][0]
    assert "Сектор: Operations" in card_message.edits[-1][0]

    await bot.admin_user_role(FakeCallback(f"urol:h:{target.id}", card_message))
    await session.refresh(target)
    assert target.role == Role.SECTOR_HEAD
    assert "Роль: Председатель сектора" in card_message.edits[-1][0]

    await bot.admin_user_status(FakeCallback(f"ustat:{target.id}", card_message))
    await session.refresh(target)
    assert target.status == UserStatus.INACTIVE
    assert "Статус: inactive" in card_message.edits[-1][0]

    await bot.admin_user_status(FakeCallback(f"ustat:{target.id}", card_message))
    await session.refresh(target)
    assert target.status == UserStatus.ACTIVE
    assert await session.scalar(
        select(func.count()).select_from(AuditLog).where(AuditLog.action == "admin.user_updated")
    ) == 3


@pytest.mark.asyncio
async def test_admin_handler_guards_authorization_validation_and_self_lockout(
    session, monkeypatch
) -> None:
    admin = user(1060, "Admin", role=Role.ADMIN)
    target = user(1061, "Target")
    outsider = user(1062, "Outsider")
    inactive_sector = Sector(name="Inactive", is_active=False)
    session.add_all([admin, target, outsider, inactive_sector])
    await session.commit()
    use_existing_session(monkeypatch, session)

    async def sync_admin(_update):
        return admin

    monkeypatch.setattr(bot, "sync_callback_user", sync_admin)
    message = FakeMessage()

    no_sector = FakeCallback(f"urol:h:{target.id}", message)
    await bot.admin_user_role(no_sector)
    await session.refresh(target)
    assert target.role == Role.PARTICIPANT
    assert "active sector" in no_sector.answers[-1][0]

    self_lockout = FakeCallback(f"ustat:{admin.id}", message)
    await bot.admin_user_status(self_lockout)
    await session.refresh(admin)
    assert admin.status == UserStatus.ACTIVE
    assert "cannot deactivate" in self_lockout.answers[-1][0]

    state = FakeState(admin_sector_user_id=str(target.id))
    inactive = FakeCallback(f"us:{inactive_sector.id}", message)
    await bot.admin_user_sector_finish(inactive, state)
    assert "active sector" in inactive.answers[-1][0]
    assert target.sector_id is None

    async def sync_outsider(_update):
        return outsider

    monkeypatch.setattr(bot, "sync_callback_user", sync_outsider)
    unauthorized = FakeCallback(f"usector:{target.id}", message)
    await bot.admin_user_sector_start(unauthorized, FakeState())
    assert unauthorized.answers[-1] == ("Недостаточно прав.", True)


@pytest.mark.asyncio
async def test_task_full_directory_paginates_both_ways_and_keeps_selection(monkeypatch) -> None:
    actor = user(1, "Admin", role=Role.ADMIN)
    first = user(2, "First")
    second = user(3, "Second")
    state = FakeState(member_ids=[str(first.id)], people_picker_mode="all", people_picker_page=0)
    message = FakeMessage()

    async def sync_actor(_callback):
        return actor

    async def page(_actor, requested_page, **_kwargs):
        return ([first] if requested_page == 0 else [second], requested_page, 2)

    monkeypatch.setattr(bot, "sync_callback_user", sync_actor)
    monkeypatch.setattr(bot, "active_users_page", page)

    forward = FakeCallback("issue:people:all:1", message)
    await bot.issue_task_people_all(forward, state)
    assert state.data["people_picker_page"] == 1
    assert message.reply_markup.inline_keyboard[-2][0].text == "← Назад"
    assert message.reply_markup.inline_keyboard[-1][0].text.endswith("(1)")

    backward = FakeCallback("issue:people:all:0", message)
    await bot.issue_task_people_all(backward, state)
    assert state.data["people_picker_page"] == 0
    assert message.reply_markup.inline_keyboard[0][0].text.startswith("✅")
    assert message.reply_markup.inline_keyboard[-2][0].text == "Вперёд →"


@pytest.mark.asyncio
async def test_task_directory_selects_and_deselects_on_same_page(monkeypatch) -> None:
    actor = user(10, "Admin", role=Role.ADMIN)
    candidate = user(11, "Candidate")
    state = FakeState(member_ids=[], people_picker_mode="all", people_picker_page=0)
    message = FakeMessage(
        reply_markup=bot.people_all_keyboard(
            [candidate],
            set(),
            0,
            1,
            person_callback_prefix="issue:person",
            done_callback="issue:people:done",
            page_callback_prefix="issue:people:all",
        )
    )

    async def sync_actor(_callback):
        return actor

    async def page(*_args, **_kwargs):
        return [candidate], 0, 1

    monkeypatch.setattr(bot, "sync_callback_user", sync_actor)
    monkeypatch.setattr(bot, "active_users_page", page)
    callback = FakeCallback(f"issue:person:{candidate.id}", message)

    await bot.issue_task_person(callback, state)
    assert state.data["member_ids"] == [str(candidate.id)]
    assert message.reply_markup.inline_keyboard[0][0].text.startswith("✅")

    await bot.issue_task_person(callback, state)
    assert state.data["member_ids"] == []
    assert message.reply_markup.inline_keyboard[0][0].text.startswith("➕")


@pytest.mark.asyncio
@pytest.mark.parametrize("query", ["Анна Тест", "@USER21"])
async def test_task_search_exact_name_or_username_adds_immediately(monkeypatch, query) -> None:
    actor = user(20, "Admin", role=Role.ADMIN)
    candidate = user(21, "Анна Тест")
    state = FakeState(member_ids=[])
    message = FakeMessage(query)

    async def sync_actor(_message):
        return actor

    async def search(*_args, **_kwargs):
        return [candidate]

    monkeypatch.setattr(bot, "sync_user", sync_actor)
    monkeypatch.setattr(bot, "search_users", search)
    await bot.issue_task_people(message, state)

    assert state.data["member_ids"] == [str(candidate.id)]
    assert "Всего: 1" in message.answers[-1][0]


@pytest.mark.asyncio
async def test_task_search_ambiguous_and_empty_results_are_safe(monkeypatch) -> None:
    actor = user(30, "Admin", role=Role.ADMIN)
    matches = [user(31, "Анна Первая"), user(32, "Анна Вторая")]

    async def sync_actor(_message):
        return actor

    monkeypatch.setattr(bot, "sync_user", sync_actor)

    async def ambiguous(*_args, **_kwargs):
        return matches

    monkeypatch.setattr(bot, "search_users", ambiguous)
    message = FakeMessage("Анна")
    await bot.issue_task_people(message, FakeState(member_ids=[]))
    assert len(message.answers[-1][1].inline_keyboard) == 3

    async def empty(*_args, **_kwargs):
        return []

    monkeypatch.setattr(bot, "search_users", empty)
    message = FakeMessage("Nobody")
    await bot.issue_task_people(message, FakeState(member_ids=[]))
    assert "Никого не найдено" in message.answers[-1][0]


@pytest.mark.asyncio
async def test_task_picker_rejects_invalid_counts_and_stale_or_cross_sector_users(monkeypatch) -> None:
    message = FakeMessage()
    assert await bot.finish_task_people(message, FakeState(kind="individual", member_ids=[])) is False
    assert await bot.finish_task_people(
        message,
        FakeState(kind="individual", member_ids=[str(uuid.uuid4()), str(uuid.uuid4())]),
    ) is False

    sector = uuid.uuid4()
    actor = user(40, "Head", role=Role.SECTOR_HEAD, sector_id=sector)
    unavailable = user(41, "Inactive", active=False, sector_id=sector)
    outsider = user(42, "Outsider", sector_id=uuid.uuid4())

    async def sync_actor(_callback):
        return actor

    monkeypatch.setattr(bot, "sync_callback_user", sync_actor)
    for candidate, expected in [
        (unavailable, "больше недоступен"),
        (outsider, "только участников своего сектора"),
    ]:
        state = FakeState(member_ids=[], people_picker_mode="all", people_picker_page=0)
        message = FakeMessage(
                reply_markup=bot.people_all_keyboard(
                    [candidate],
                    set(),
                    0,
                    1,
                    person_callback_prefix="issue:person",
                    done_callback="issue:people:done",
                    page_callback_prefix="issue:people:all",
            )
        )

        async def page(*_args, current=candidate, **_kwargs):
            return [current], 0, 1

        monkeypatch.setattr(bot, "active_users_page", page)
        callback = FakeCallback(f"issue:person:{candidate.id}", message)
        await bot.issue_task_person(callback, state)
        assert expected in callback.answers[-1][0]
        assert state.data["member_ids"] == []


@pytest.mark.asyncio
async def test_event_directory_selection_persists_deselects_and_finishes(monkeypatch) -> None:
    actor = user(50, "Admin", role=Role.ADMIN)
    candidate = user(51, "Participant")
    state = FakeState(participant_ids=[], people_picker_mode="all", people_picker_page=0)
    message = FakeMessage()

    async def sync_actor(_callback):
        return actor

    async def page(*_args, **_kwargs):
        return [candidate], 0, 2

    monkeypatch.setattr(bot, "sync_callback_user", sync_actor)
    monkeypatch.setattr(bot, "active_users_page", page)
    await bot.event_people_all(FakeCallback("evp:all:0", message), state)
    assert message.reply_markup.inline_keyboard[-2][0].callback_data == "evp:all:1"

    callback = FakeCallback(f"evp:{candidate.id}", message)
    await bot.event_create_people_callback(callback, state)
    assert state.data["participant_ids"] == [str(candidate.id)]
    assert message.reply_markup.inline_keyboard[-1][0].text.endswith("(1)")
    await bot.event_create_people_callback(callback, state)
    assert state.data["participant_ids"] == []

    completed: dict[str, object] = {}

    async def create_event(message_arg, state_arg, actor_arg):
        completed.update(message=message_arg, state=state_arg, actor=actor_arg)

    monkeypatch.setattr(bot, "create_event_from_state", create_event)
    await bot.event_create_people_callback(FakeCallback("evp:done", message), state)
    assert completed == {"message": message, "state": state, "actor": actor}


@pytest.mark.asyncio
async def test_event_picker_rejects_stale_and_cross_sector_callbacks(monkeypatch) -> None:
    sector = uuid.uuid4()
    actor = user(60, "Head", role=Role.SECTOR_HEAD, sector_id=sector)

    async def sync_actor(_callback):
        return actor

    monkeypatch.setattr(bot, "sync_callback_user", sync_actor)
    for candidate, expected in [
        (user(61, "Inactive", active=False, sector_id=sector), "больше недоступен"),
        (user(62, "Outsider", sector_id=uuid.uuid4()), "только участников своего сектора"),
    ]:
        state = FakeState(participant_ids=[], people_picker_mode="all", people_picker_page=0)
        message = FakeMessage(
            reply_markup=bot.people_all_keyboard(
                [candidate],
                set(),
                0,
                1,
                person_callback_prefix="evp",
                done_callback="evp:done",
                page_callback_prefix="evp:all",
            )
        )

        async def page(*_args, current=candidate, **_kwargs):
            return [current], 0, 1

        monkeypatch.setattr(bot, "active_users_page", page)
        callback = FakeCallback(f"evp:{candidate.id}", message)
        await bot.event_create_people_callback(callback, state)
        assert expected in callback.answers[-1][0]
        assert state.data["participant_ids"] == []


def test_generated_picker_callback_payloads_fit_telegram_limit() -> None:
    candidate = user(70, "Callback User")
    keyboards = [
        bot.people_search_keyboard([candidate], {str(candidate.id)}),
        bot.people_all_keyboard(
            [candidate],
            set(),
            999,
            1001,
            person_callback_prefix="issue:person",
            done_callback="issue:people:done",
            page_callback_prefix="issue:people:all",
        ),
        bot.event_people_keyboard([candidate], set()),
    ]
    callbacks = [
        button.callback_data
        for keyboard in keyboards
        for row in keyboard.inline_keyboard
        for button in row
        if button.callback_data
    ]
    assert callbacks
    assert max(len(value.encode("utf-8")) for value in callbacks) <= 64


@pytest.mark.asyncio
async def test_telegram_photo_and_image_document_download_contract() -> None:
    class FakeBot:
        def __init__(self, payload: bytes):
            self.payload = payload

        async def get_file(self, file_id):
            return SimpleNamespace(file_path=f"files/{file_id}")

        async def download_file(self, _path, destination):
            destination.write(self.payload)

    photo_message = SimpleNamespace(
        photo=[SimpleNamespace(file_size=4, file_id="photo")],
        document=None,
        bot=FakeBot(b"jpeg"),
    )
    document_message = SimpleNamespace(
        photo=None,
        document=SimpleNamespace(
            file_size=3,
            file_id="document",
            mime_type="image/png",
        ),
        bot=FakeBot(b"png"),
    )

    assert await bot.download_image_bytes(photo_message) == (b"jpeg", "image/jpeg")
    assert await bot.download_image_bytes(document_message) == (b"png", "image/png")


@pytest.mark.asyncio
async def test_telegram_photo_download_rejects_wrong_type_oversize_and_empty_file() -> None:
    unsupported = SimpleNamespace(
        photo=None,
        document=SimpleNamespace(file_size=1, file_id="x", mime_type="application/pdf"),
        bot=None,
    )
    oversized = SimpleNamespace(
        photo=[SimpleNamespace(file_size=10 * 1024 * 1024 + 1, file_id="large")],
        document=None,
        bot=None,
    )

    class EmptyBot:
        async def get_file(self, _file_id):
            return SimpleNamespace(file_path="empty")

        async def download_file(self, _path, destination):
            destination.write(b"")

    empty = SimpleNamespace(
        photo=[SimpleNamespace(file_size=0, file_id="empty")],
        document=None,
        bot=EmptyBot(),
    )

    with pytest.raises(ValueError, match="JPEG"):
        await bot.download_image_bytes(unsupported)
    with pytest.raises(ValueError, match="10 МБ"):
        await bot.download_image_bytes(oversized)
    with pytest.raises(ValueError, match="1 байта"):
        await bot.download_image_bytes(empty)


@pytest.mark.asyncio
async def test_admin_can_assign_sector_through_telegram_callbacks(session, monkeypatch) -> None:
    actor = user(80, "Admin", role=Role.ADMIN)
    target = user(81, "Target")
    sector = Sector(name="Active Sector")
    session.add_all([actor, target, sector])
    await session.commit()

    async def sync_actor(_callback):
        return actor

    use_existing_session(monkeypatch, session)
    monkeypatch.setattr(bot, "sync_callback_user", sync_actor)
    state = FakeState()
    message = FakeMessage()
    await bot.admin_user_sector_start(FakeCallback(f"usector:{target.id}", message), state)

    assert state.state == bot.AdminUserSector.choose
    assert state.data["admin_sector_user_id"] == str(target.id)
    callbacks = [button.callback_data for row in message.reply_markup.inline_keyboard for button in row]
    assert f"us:{sector.id}" in callbacks
    assert max(len(value.encode("utf-8")) for value in callbacks) <= 64

    await bot.admin_user_sector_finish(FakeCallback(f"us:{sector.id}", message), state)
    await session.refresh(target)
    assert target.sector_id == sector.id
    assert state.state is None
    assert "Сектор: Active Sector" in message.edits[-1][0]
    audit_log = await session.scalar(
        select(AuditLog).where(
            AuditLog.action == "admin.user_updated",
            AuditLog.entity_id == str(target.id),
        )
    )
    assert audit_log is not None
    assert audit_log.details["new"]["sector_id"] == str(sector.id)


@pytest.mark.asyncio
async def test_sector_picker_rejects_inactive_forged_missing_and_removed_targets(
    session, monkeypatch
) -> None:
    actor = user(1080, "Admin", role=Role.ADMIN)
    target = user(1081, "Target")
    active_sector = Sector(name="Temporary")
    inactive_sector = Sector(name="Disabled", is_active=False)
    session.add_all([actor, target, active_sector, inactive_sector])
    await session.commit()
    use_existing_session(monkeypatch, session)

    async def sync_actor(_callback):
        return actor

    monkeypatch.setattr(bot, "sync_callback_user", sync_actor)
    state = FakeState()
    message = FakeMessage()
    await bot.admin_user_sector_start(FakeCallback(f"usector:{target.id}", message), state)
    callbacks = {
        button.callback_data for row in message.reply_markup.inline_keyboard for button in row
    }
    assert f"us:{active_sector.id}" in callbacks
    assert f"us:{inactive_sector.id}" not in callbacks

    forged_state = FakeState(admin_sector_user_id=str(target.id))
    forged = FakeCallback("us:not-a-uuid", message)
    await bot.admin_user_sector_finish(forged, forged_state)
    assert forged.answers[-1][1] is True
    assert forged_state.state is None

    removed_sector_id = active_sector.id
    await session.delete(active_sector)
    await session.commit()
    removed = FakeCallback(f"us:{removed_sector_id}", message)
    await bot.admin_user_sector_finish(
        removed,
        FakeState(admin_sector_user_id=str(target.id)),
    )
    assert "active sector" in removed.answers[-1][0]
    assert target.sector_id is None

    missing = FakeCallback("us:none", message)
    await bot.admin_user_sector_finish(
        missing,
        FakeState(admin_sector_user_id=str(uuid.uuid4())),
    )
    assert "User not found" in missing.answers[-1][0]


@pytest.mark.asyncio
async def test_group_task_wizard_persists_creator_led_task_end_to_end(session, monkeypatch) -> None:
    actor = user(90, "Wizard Admin", role=Role.ADMIN)
    assignee = user(91, "Wizard Assignee")
    session.add_all([actor, assignee])
    await session.commit()

    class ExistingSession:
        async def __aenter__(self):
            return session

        async def __aexit__(self, exc_type, exc, traceback):
            return False

    async def sync_actor(_update):
        return actor

    async def search(*_args, **_kwargs):
        return [assignee]

    monkeypatch.setattr(bot, "SessionLocal", lambda: ExistingSession())
    monkeypatch.setattr(bot, "sync_user", sync_actor)
    monkeypatch.setattr(bot, "sync_callback_user", sync_actor)
    monkeypatch.setattr(bot, "search_users", search)
    state = FakeState()
    message = FakeMessage()

    await bot.issue_task_start(message, state)
    await bot.issue_task_title(FakeMessage("Wizard group"), state)
    await bot.issue_task_description(FakeMessage("Full handler flow"), state)
    await bot.issue_task_event(FakeCallback("issue:event:none", message), state)
    await bot.issue_task_kind(FakeCallback("issue:kind:group", message), state)
    await bot.issue_task_people(FakeMessage("Wizard Assignee"), state)
    await bot.issue_task_people_done(FakeCallback("issue:people:done", message), state)
    await bot.issue_task_leader(FakeCallback(f"issue:leader:{actor.id}", message), state)
    await bot.issue_task_deadline(FakeMessage("31.12.2027 12:00"), state)
    await bot.issue_task_checklist(FakeMessage("First item\nSecond item"), state)

    task = await session.scalar(select(Task).where(Task.title == "Wizard group"))
    assert task is not None
    assert task.creator_id == actor.id
    assert task.leader_id == actor.id
    members = list(
        (await session.scalars(select(TaskMember).where(TaskMember.task_id == task.id))).all()
    )
    assert {member.user_id for member in members} == {actor.id, assignee.id}
    creator_member = next(member for member in members if member.user_id == actor.id)
    assert creator_member.is_creator is True
    assert creator_member.is_leader is True
    assert await session.scalar(select(TaskChat).where(TaskChat.task_id == task.id)) is not None
    assert len(
        list(
            (
                await session.scalars(
                    select(TaskChecklistItem).where(TaskChecklistItem.task_id == task.id)
                )
            ).all()
        )
    ) == 2
    outbox = list(
        (await session.scalars(select(OutboxEvent).where(OutboxEvent.aggregate_id == str(task.id)))).all()
    )
    assert [event.event_type for event in outbox] == ["TASK_CREATED"]
    assert state.state is None


@pytest.mark.asyncio
async def test_checklist_task_card_toggle_metadata_and_authorization(session, monkeypatch) -> None:
    manager = user(1100, "Manager", role=Role.ADMIN)
    member = user(1101, "Member")
    outsider = user(1102, "Outsider")
    task = Task(
        title="Checklist task",
        description="Verify the bot flow",
        kind=TaskKind.INDIVIDUAL,
        status=TaskStatus.ACTIVE,
        deadline=datetime.now(UTC) + timedelta(days=2),
        creator_id=manager.id,
        idempotency_key="checklist-flow",
    )
    session.add_all([manager, member, outsider, task])
    await session.flush()
    session.add_all(
        [
            TaskMember(task_id=task.id, user_id=manager.id, is_creator=True),
            TaskMember(task_id=task.id, user_id=member.id),
        ]
    )
    item = TaskChecklistItem(task_id=task.id, title="First item", position=0)
    session.add(item)
    await session.commit()
    use_existing_session(monkeypatch, session)

    card_text, card_keyboard = await bot.build_task_view(member, task.id)
    assert "Чек-лист: 0/1" in card_text
    callbacks = {
        button.callback_data for row in card_keyboard.inline_keyboard for button in row
    }
    assert f"check:{item.id}" in callbacks

    shown: list[tuple[uuid.UUID, uuid.UUID]] = []

    async def show_task(_target, actor, task_id):
        shown.append((actor.id, task_id))

    async def sync_member(_callback):
        return member

    monkeypatch.setattr(bot, "show_task", show_task)
    monkeypatch.setattr(bot, "sync_callback_user", sync_member)
    callback = FakeCallback(f"check:{item.id}", FakeMessage())
    await bot.toggle_checklist(callback)
    await session.refresh(item)
    assert item.is_completed is True
    assert item.completed_by_id == member.id
    assert item.completed_at is not None
    assert shown[-1] == (member.id, task.id)

    await bot.toggle_checklist(callback)
    await session.refresh(item)
    assert item.is_completed is False
    assert item.completed_by_id is None
    assert item.completed_at is None

    async def sync_outsider(_callback):
        return outsider

    monkeypatch.setattr(bot, "sync_callback_user", sync_outsider)
    denied = FakeCallback(f"check:{item.id}", FakeMessage())
    await bot.toggle_checklist(denied)
    assert "membership" in denied.answers[-1][0]

    forged = FakeCallback("check:not-a-uuid", FakeMessage())
    await bot.toggle_checklist(forged)
    assert forged.answers[-1][0] == "Операция не выполнена. Попробуйте ещё раз."


@pytest.mark.asyncio
async def test_checklist_manager_add_delete_and_stale_callbacks(session, monkeypatch) -> None:
    manager = user(1110, "Manager", role=Role.ADMIN)
    participant = user(1111, "Participant")
    task = Task(
        title="Managed checklist",
        kind=TaskKind.GROUP,
        status=TaskStatus.ACTIVE,
        deadline=datetime.now(UTC) + timedelta(days=2),
        creator_id=manager.id,
        leader_id=manager.id,
        idempotency_key="managed-checklist",
    )
    session.add_all([manager, participant, task])
    await session.flush()
    session.add_all(
        [
            TaskMember(
                task_id=task.id,
                user_id=manager.id,
                is_creator=True,
                is_leader=True,
            ),
            TaskMember(task_id=task.id, user_id=participant.id),
        ]
    )
    await session.commit()
    use_existing_session(monkeypatch, session)

    async def sync_manager(_update):
        return manager

    async def ignore_show(*_args):
        return None

    monkeypatch.setattr(bot, "sync_callback_user", sync_manager)
    monkeypatch.setattr(bot, "sync_user", sync_manager)
    monkeypatch.setattr(bot, "show_task", ignore_show)

    state = FakeState()
    start_callback = FakeCallback(f"cladd:{task.id}", FakeMessage())
    await bot.checklist_add_start(start_callback, state)
    assert state.state == bot.ChecklistAdd.title
    await bot.checklist_add_finish(FakeMessage("New item"), state)
    added = await session.scalar(
        select(TaskChecklistItem).where(
            TaskChecklistItem.task_id == task.id,
            TaskChecklistItem.title == "New item",
        )
    )
    assert added is not None
    assert state.state is None

    await bot.checklist_delete(FakeCallback(f"cldel:{added.id}", FakeMessage()))
    assert await session.get(TaskChecklistItem, added.id) is None

    stale = FakeCallback(f"cldel:{uuid.uuid4()}", FakeMessage())
    await bot.checklist_delete(stale)
    assert "Пункт не найден" in stale.answers[-1][0]

    async def sync_participant(_update):
        return participant

    monkeypatch.setattr(bot, "sync_callback_user", sync_participant)
    denied_state = FakeState()
    denied = FakeCallback(f"cladd:{task.id}", FakeMessage())
    await bot.checklist_add_start(denied, denied_state)
    assert denied.answers[-1][0] == "Нет доступа"
    assert denied_state.state is None


@pytest.mark.asyncio
async def test_event_pdf_handler_delivers_valid_document_and_enforces_access(
    session, monkeypatch
) -> None:
    manager = user(1120, "Manager", role=Role.ADMIN)
    participant = user(1121, "Participant")
    outsider = user(1122, "Outsider")
    event = Event(
        title="Release Event",
        starts_at=datetime.now(UTC),
        ends_at=datetime.now(UTC) + timedelta(hours=2),
        created_by_id=manager.id,
    )
    session.add_all([manager, participant, outsider, event])
    await session.flush()
    task = Task(
        event_id=event.id,
        title="Archived task",
        kind=TaskKind.INDIVIDUAL,
        status=TaskStatus.COMPLETED,
        deadline=datetime.now(UTC) + timedelta(days=1),
        creator_id=manager.id,
        completed_at=datetime.now(UTC),
        idempotency_key="archive-pdf",
    )
    session.add(task)
    await session.flush()
    session.add_all(
        [
            EventParticipant(event_id=event.id, user_id=participant.id),
            TaskMember(task_id=task.id, user_id=manager.id, is_creator=True),
            TaskMember(task_id=task.id, user_id=participant.id),
            TaskReport(
                task_id=task.id,
                submitted_by_id=participant.id,
                status=ReportStatus.APPROVED,
                comment="Completed",
            ),
        ]
    )
    await session.commit()
    use_existing_session(monkeypatch, session)

    async def sync_manager(_callback):
        return manager

    seen_event_ids: list[uuid.UUID] = []
    real_pdf_builder = bot.build_event_archive_pdf

    def record_pdf(event_arg, participants_arg, tasks_arg):
        seen_event_ids.append(event_arg.id)
        return real_pdf_builder(event_arg, participants_arg, tasks_arg)

    monkeypatch.setattr(bot, "sync_callback_user", sync_manager)
    monkeypatch.setattr(bot, "build_event_archive_pdf", record_pdf)
    message = FakeMessage()
    callback = FakeCallback(f"epdf:{event.id}", message)
    await bot.event_pdf(callback)
    assert seen_event_ids == [event.id]
    assert len(message.documents) == 1
    document = message.documents[0]
    assert document.filename == f"event-{event.id}-archive.pdf"
    assert len(document.data) > 500
    assert document.data.startswith(b"%PDF-")

    async def sync_outsider(_callback):
        return outsider

    monkeypatch.setattr(bot, "sync_callback_user", sync_outsider)
    denied_message = FakeMessage()
    denied = FakeCallback(f"epdf:{event.id}", denied_message)
    await bot.event_pdf(denied)
    assert denied_message.documents == []
    assert denied.answers[-1][0]

    event.purged_at = datetime.now(UTC)
    await session.commit()
    monkeypatch.setattr(bot, "sync_callback_user", sync_manager)
    purged = FakeCallback(f"epdf:{event.id}", FakeMessage())
    await bot.event_pdf(purged)
    assert "Срок хранения" in purged.answers[-1][0]


@pytest.mark.asyncio
async def test_event_zip_handler_scopes_photos_sanitizes_names_and_handles_failures(
    session, monkeypatch
) -> None:
    manager = user(1130, "Manager", role=Role.ADMIN)
    participant = user(1131, "Participant")
    event = Event(title="First", starts_at=datetime.now(UTC), created_by_id=manager.id)
    other_event = Event(title="Second", starts_at=datetime.now(UTC), created_by_id=manager.id)
    empty_event = Event(title="Empty", starts_at=datetime.now(UTC), created_by_id=manager.id)
    session.add_all([manager, participant, event, other_event, empty_event])
    await session.flush()
    tasks = [
        Task(
            event_id=current_event.id,
            title=current_event.title,
            kind=TaskKind.INDIVIDUAL,
            status=TaskStatus.COMPLETED,
            deadline=datetime.now(UTC) + timedelta(days=1),
            creator_id=manager.id,
            idempotency_key=f"archive-zip-{index}",
        )
        for index, current_event in enumerate([event, other_event])
    ]
    session.add_all(tasks)
    await session.flush()
    reports = [
        TaskReport(
            task_id=task.id,
            submitted_by_id=participant.id,
            status=ReportStatus.APPROVED,
        )
        for task in tasks
    ]
    session.add_all(reports)
    await session.flush()
    photos = [
        TaskPhoto(
            report_id=reports[0].id,
            object_key="event-one/..\\unsafe name.jpg",
            content_type="image/jpeg",
            size_bytes=3,
            uploaded_by_id=participant.id,
        ),
        TaskPhoto(
            report_id=reports[1].id,
            object_key="event-two/other.jpg",
            content_type="image/jpeg",
            size_bytes=5,
            uploaded_by_id=participant.id,
        ),
    ]
    session.add_all(photos)
    await session.commit()
    use_existing_session(monkeypatch, session)

    async def sync_manager(_callback):
        return manager

    monkeypatch.setattr(bot, "sync_callback_user", sync_manager)
    payloads = {
        photos[0].object_key: b"one",
        photos[1].object_key: b"other",
    }
    monkeypatch.setattr(archive_module, "get_object_bytes", payloads.__getitem__)

    message = FakeMessage()
    await bot.event_zip(FakeCallback(f"ezip:{event.id}", message))
    assert len(message.documents) == 1
    with zipfile.ZipFile(BytesIO(message.documents[0].data)) as archive:
        assert len(archive.namelist()) == 1
        filename = archive.namelist()[0]
        assert "/" not in filename and "\\" not in filename and ".." not in filename
        assert archive.read(filename) == b"one"

    empty_message = FakeMessage()
    await bot.event_zip(FakeCallback(f"ezip:{empty_event.id}", empty_message))
    with zipfile.ZipFile(BytesIO(empty_message.documents[0].data)) as archive:
        assert archive.namelist() == []

    def storage_failure(_key):
        raise RuntimeError("storage unavailable")

    monkeypatch.setattr(archive_module, "get_object_bytes", storage_failure)
    failure_message = FakeMessage()
    failure = FakeCallback(f"ezip:{event.id}", failure_message)
    await bot.event_zip(failure)
    assert failure_message.documents == []
    assert failure.answers[-1][0] == "Операция не выполнена. Попробуйте ещё раз."


def test_callback_contract_all_generated_actions_are_reachable_and_safe() -> None:
    source_path = Path(bot.__file__)
    source = source_path.read_text(encoding="utf-8")
    tree = ast.parse(source)
    sample_uuid = str(uuid.UUID("12345678-1234-5678-9234-567812345678"))

    handlers: list[tuple[str, str, int]] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)):
            continue
        for decorator in node.decorator_list:
            for expression in ast.walk(decorator):
                if (
                    isinstance(expression, ast.Call)
                    and isinstance(expression.func, ast.Attribute)
                    and expression.func.attr == "startswith"
                    and expression.args
                    and isinstance(expression.args[0], ast.Constant)
                ):
                    handlers.append(("prefix", expression.args[0].value, node.lineno))
                if (
                    isinstance(expression, ast.Compare)
                    and len(expression.ops) == 1
                    and isinstance(expression.ops[0], ast.Eq)
                    and len(expression.comparators) == 1
                    and isinstance(expression.comparators[0], ast.Constant)
                    and isinstance(expression.comparators[0].value, str)
                ):
                    handlers.append(("exact", expression.comparators[0].value, node.lineno))

    samples: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        callback_value = next(
            (keyword.value for keyword in node.keywords if keyword.arg == "callback_data"),
            None,
        )
        if isinstance(callback_value, ast.Constant) and isinstance(callback_value.value, str):
            samples.add(callback_value.value)
        elif isinstance(callback_value, ast.JoinedStr):
            rendered = ""
            skip = False
            for part in callback_value.values:
                if isinstance(part, ast.Constant):
                    rendered += str(part.value)
                    continue
                expression = ast.get_source_segment(source, part.value) or ""
                if expression in {
                    "person_callback_prefix",
                    "page_callback_prefix",
                }:
                    skip = True
                    break
                if expression == "action":
                    rendered += "retry"
                elif "page" in expression:
                    rendered += "0"
                else:
                    rendered += sample_uuid
            if not skip:
                samples.add(rendered)

    candidate = user(1140, "Callback User")
    dynamic_keyboards = [
        bot.people_search_keyboard([candidate], set()),
        bot.people_all_keyboard(
            [candidate],
            set(),
            0,
            2,
            person_callback_prefix="issue:person",
            done_callback="issue:people:done",
            page_callback_prefix="issue:people:all",
        ),
        bot.event_people_keyboard([candidate], set()),
        bot.people_picker_mode_keyboard(
            all_callback="evp:all:0",
            search_callback="evp:search",
            done_callback="evp:done",
        ),
    ]
    samples.update(
        button.callback_data
        for keyboard in dynamic_keyboards
        for row in keyboard.inline_keyboard
        for button in row
        if button.callback_data
    )
    samples.add(f"chrecover:{sample_uuid}")

    assert len(handlers) == len({(kind, value) for kind, value, _line in handlers})
    for kind, value, line in handlers:
        if kind != "prefix":
            continue
        more_specific = [
            other_line
            for other_kind, other_value, other_line in handlers
            if other_line != line
            and other_value != value
            and other_value.startswith(value)
            and other_kind in {"exact", "prefix"}
        ]
        assert all(line > other_line for other_line in more_specific), (
            f"generic handler {value!r} shadows a more specific handler"
        )

    assert samples
    for callback_data in samples:
        assert len(callback_data.encode("utf-8")) <= 64
        assert not {"miniapp", "webapp", "vite", "react"} & set(
            callback_data.casefold().replace(":", " ").split()
        )
        assert any(
            callback_data == value if kind == "exact" else callback_data.startswith(value)
            for kind, value, _line in handlers
        ), f"unreachable callback_data: {callback_data}"
        for segment in callback_data.split(":"):
            if len(segment) == 36 and segment.count("-") == 4:
                assert str(uuid.UUID(segment)) == segment


@pytest.mark.asyncio
async def test_bot_runtime_initializes_router_with_fake_telegram(monkeypatch) -> None:
    calls: list[object] = []

    class FakeBot:
        def __init__(self):
            self.session = SimpleNamespace(close=self.close)

        async def delete_webhook(self, *, drop_pending_updates):
            calls.append(("delete_webhook", drop_pending_updates))

        async def close(self):
            calls.append("close")

    fake_bot = FakeBot()

    async def start_polling(_dispatcher, polling_bot, **kwargs):
        calls.append(("start_polling", polling_bot, kwargs["allowed_updates"]))

    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123456:release-test-token")
    monkeypatch.setattr(
        bot,
        "get_settings",
        lambda: SimpleNamespace(
            log_level="INFO",
            telegram_webhook_url="",
            telegram_webhook_path="/webhook",
            telegram_webhook_secret="",
        ),
    )
    monkeypatch.setattr(bot, "build_telegram_bot", lambda _token: fake_bot)
    monkeypatch.setattr(bot.Dispatcher, "start_polling", start_polling)

    await bot.run()

    assert calls[0] == ("delete_webhook", False)
    assert calls[1] == (
        "start_polling",
        fake_bot,
        ["message", "callback_query", "chat_member"],
    )
    assert calls[2] == "close"
