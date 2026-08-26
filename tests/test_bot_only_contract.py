from pathlib import Path

from apps.api.app.models import Role, User, UserStatus
from apps.bot.app.main import event_people_keyboard, main_keyboard, people_keyboard


def user(telegram_id: int, role: Role) -> User:
    value = User(
        telegram_id=telegram_id,
        telegram_username=f"user{telegram_id}",
        full_name=f"User {telegram_id}",
        normalized_full_name=f"user {telegram_id}",
        role=role,
        status=UserStatus.ACTIVE,
    )
    return value


def keyboard_texts(markup) -> list[str]:
    return [button.text for row in markup.keyboard for button in row]


def inline_texts(markup) -> list[str]:
    return [button.text for row in markup.inline_keyboard for button in row]


def test_participant_menu_is_bot_native() -> None:
    texts = keyboard_texts(main_keyboard(user(1, Role.PARTICIPANT)))
    assert "📋 Мои задачи" in texts
    assert "👤 Профиль" in texts
    assert "➕ Выдать задачу" not in texts
    assert "🗓 Мероприятия" not in texts
    assert "⚙️ Администрирование" not in texts


def test_manager_and_admin_actions_are_available_in_bot() -> None:
    sector_head_texts = keyboard_texts(main_keyboard(user(2, Role.SECTOR_HEAD)))
    assert "➕ Выдать задачу" in sector_head_texts
    assert "🗓 Мероприятия" in sector_head_texts
    assert "⚙️ Администрирование" not in sector_head_texts

    admin_texts = keyboard_texts(main_keyboard(user(3, Role.ADMIN)))
    assert "➕ Выдать задачу" in admin_texts
    assert "🗓 Мероприятия" in admin_texts
    assert "⚙️ Администрирование" in admin_texts


def test_people_selection_shows_full_name_and_username() -> None:
    first = user(101, Role.PARTICIPANT)
    second = user(102, Role.PARTICIPANT)
    first.id = __import__("uuid").uuid4()
    second.id = __import__("uuid").uuid4()

    task_markup = people_keyboard([first, second], {str(first.id)})
    event_markup = event_people_keyboard([first, second], {str(second.id)})

    task_text = "\n".join(inline_texts(task_markup))
    event_text = "\n".join(inline_texts(event_markup))
    assert "User 101" in task_text and "@user101" in task_text
    assert "User 102" in event_text and "@user102" in event_text


def test_runtime_and_ci_do_not_depend_on_miniapp() -> None:
    compose = Path("docker-compose.yml").read_text(encoding="utf-8")
    production = Path("docker-compose.production.yml").read_text(encoding="utf-8")
    caddy = Path("Caddyfile").read_text(encoding="utf-8")
    ci = Path(".github/workflows/ci.yml").read_text(encoding="utf-8")

    assert not Path("apps/miniapp").exists()
    assert "miniapp:" not in compose
    assert "miniapp:" not in production
    assert "reverse_proxy miniapp" not in caddy
    assert "working-directory: apps/miniapp" not in ci
    assert "npm run build" not in ci
