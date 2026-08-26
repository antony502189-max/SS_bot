from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import Message
from sqlalchemy import select

from apps.api.app.models import User, UserStatus
from apps.bot.app import main as bot_app


class ExistingSession:
    def __init__(self, session) -> None:
        self.session = session

    async def __aenter__(self):
        return self.session

    async def __aexit__(self, exc_type, exc, traceback):
        return False


def telegram_message(text: str) -> Message:
    return Message.model_validate(
        {
            "message_id": 1,
            "date": 0,
            "chat": {"id": 971, "type": "private"},
            "from": {
                "id": 971,
                "is_bot": False,
                "first_name": "Тест",
                "username": "registration_test",
            },
            "text": text,
        }
    )


async def test_start_and_full_name_registration_use_telegram_adapter(session, monkeypatch) -> None:
    answers: list[str] = []
    storage = MemoryStorage()
    state = FSMContext(storage, StorageKey(bot_id=1, chat_id=971, user_id=971))

    async def answer(_: Message, text: str, **_kwargs) -> None:
        answers.append(text)

    monkeypatch.setattr(bot_app, "SessionLocal", lambda: ExistingSession(session))
    monkeypatch.setattr(Message, "answer", answer)

    await bot_app.start(telegram_message("/start"), state)

    assert await state.get_state() == bot_app.Registration.full_name.state
    assert answers == ["Добро пожаловать! Отправьте ваше имя и фамилию для регистрации."]

    await bot_app.receive_full_name(telegram_message("Анна Тестовая"), state)
    user = await session.scalar(select(User).where(User.telegram_id == 971))

    assert user is not None
    assert user.full_name == "Анна Тестовая"
    assert user.normalized_full_name == "анна тестовая"
    assert user.status == UserStatus.ACTIVE
    assert await state.get_state() is None
    assert answers[-1] == "Регистрация завершена. Ваш профиль готов к работе."
