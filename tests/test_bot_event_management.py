import uuid

from apps.bot.app.event_management import pack_uuid, unpack_uuid


def test_compact_uuid_round_trip() -> None:
    value = uuid.uuid4()
    packed = pack_uuid(value)
    assert len(packed) == 22
    assert unpack_uuid(packed) == value


def test_event_callback_payloads_fit_telegram_limit() -> None:
    event_id = pack_uuid(uuid.uuid4())
    user_id = pack_uuid(uuid.uuid4())
    sector_id = pack_uuid(uuid.uuid4())

    payloads = [
        f"evedit:{event_id}",
        f"evfield:t:{event_id}",
        f"evmembers:{event_id}",
        f"evaddstart:{event_id}",
        f"evadd:{event_id}:{user_id}",
        f"evrm:{event_id}:{user_id}",
        f"evsector:{event_id}",
        f"evsetsec:{event_id}:{sector_id}",
        f"evsetsec:{event_id}:none",
    ]
    assert all(len(payload.encode()) <= 64 for payload in payloads)
