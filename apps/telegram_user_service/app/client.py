from dataclasses import dataclass
from enum import StrEnum

from telethon import TelegramClient, errors, functions
from telethon.tl.types import ChatAdminRights

from apps.api.app.config import get_settings


class TelegramResultKind(StrEnum):
    SUCCESS = "success"
    NOT_JOINED = "not_joined"
    PRIVACY_RESTRICTED = "privacy_restricted"
    USERNAME_NOT_FOUND = "username_not_found"
    USER_DEACTIVATED = "user_deactivated"
    FLOOD_WAIT = "flood_wait"
    PERMISSION_ERROR = "permission_error"
    TEMPORARY_NETWORK_ERROR = "temporary_network_error"
    PERMANENT_ERROR = "permanent_error"


@dataclass(frozen=True)
class TelegramResult:
    kind: TelegramResultKind
    value: str | int | bool | None = None
    retry_after_seconds: int | None = None
    error: str | None = None


def classify_error(exc: Exception) -> TelegramResult:
    if isinstance(exc, errors.UserAlreadyParticipantError):
        return TelegramResult(TelegramResultKind.SUCCESS)
    if isinstance(exc, errors.UserNotParticipantError):
        return TelegramResult(TelegramResultKind.NOT_JOINED, error=type(exc).__name__)
    if isinstance(exc, errors.FloodWaitError):
        return TelegramResult(
            TelegramResultKind.FLOOD_WAIT, retry_after_seconds=exc.seconds, error=type(exc).__name__
        )
    if isinstance(exc, (errors.UserPrivacyRestrictedError, errors.UserNotMutualContactError)):
        return TelegramResult(TelegramResultKind.PRIVACY_RESTRICTED, error=type(exc).__name__)
    if isinstance(exc, (errors.UsernameNotOccupiedError, errors.UsernameInvalidError)):
        return TelegramResult(TelegramResultKind.USERNAME_NOT_FOUND, error=type(exc).__name__)
    if isinstance(exc, errors.UserDeactivatedError):
        return TelegramResult(TelegramResultKind.USER_DEACTIVATED, error=type(exc).__name__)
    if isinstance(exc, (errors.ChatAdminRequiredError, errors.UserAdminInvalidError)):
        return TelegramResult(TelegramResultKind.PERMISSION_ERROR, error=type(exc).__name__)
    if isinstance(exc, (OSError, TimeoutError, errors.RpcCallFailError)):
        return TelegramResult(TelegramResultKind.TEMPORARY_NETWORK_ERROR, error=type(exc).__name__)
    return TelegramResult(TelegramResultKind.PERMANENT_ERROR, error=type(exc).__name__)


class TelegramUserService:
    """MTProto service account adapter. The session file is a deployment secret."""

    def __init__(self) -> None:
        settings = get_settings()
        if not (settings.telegram_api_id and settings.telegram_api_hash):
            raise RuntimeError("TELEGRAM_API_ID and TELEGRAM_API_HASH are required")
        self.client = TelegramClient(
            str(settings.telegram_service_session_path),
            settings.telegram_api_id,
            settings.telegram_api_hash,
        )

    async def __aenter__(self):
        await self.client.connect()
        if not await self.client.is_user_authorized():
            raise RuntimeError(
                "Telegram service account must be authorized interactively before worker startup"
            )
        return self

    async def __aexit__(self, *_):
        await self.client.disconnect()

    async def create_supergroup(self, title: str, about: str) -> TelegramResult:
        try:
            result = await self.client(
                functions.channels.CreateChannelRequest(title=title, about=about, megagroup=True)
            )
            channel = next(chat for chat in result.chats if getattr(chat, "megagroup", False))
            return TelegramResult(TelegramResultKind.SUCCESS, value=channel.id)
        except Exception as exc:
            return classify_error(exc)

    async def invite_user(self, chat_id: int, username: str | None) -> TelegramResult:
        if not username:
            return TelegramResult(TelegramResultKind.USERNAME_NOT_FOUND)
        try:
            channel = await self.client.get_input_entity(chat_id)
            user = await self.client.get_input_entity(f"@{username.lstrip('@')}")
            await self.client(
                functions.channels.InviteToChannelRequest(channel=channel, users=[user])
            )
            return TelegramResult(TelegramResultKind.SUCCESS)
        except Exception as exc:
            return classify_error(exc)

    async def add_bot(self, chat_id: int, bot_username: str) -> TelegramResult:
        """Add the bot and make it a no-operational-rights admin for chat-member updates."""
        if not bot_username:
            return TelegramResult(
                TelegramResultKind.USERNAME_NOT_FOUND, error="TELEGRAM_BOT_USERNAME missing"
            )
        added = await self.invite_user(chat_id, bot_username)
        if added.kind != TelegramResultKind.SUCCESS:
            return added
        try:
            channel = await self.client.get_input_entity(chat_id)
            bot = await self.client.get_input_entity(f"@{bot_username.lstrip('@')}")
            await self.client(
                functions.channels.EditAdminRequest(
                    channel=channel,
                    user_id=bot,
                    admin_rights=ChatAdminRights(other=True),
                    rank="SS Bot",
                )
            )
            return TelegramResult(TelegramResultKind.SUCCESS)
        except Exception as exc:
            return classify_error(exc)

    async def is_user_in_chat(self, chat_id: int, username: str | None) -> TelegramResult:
        """Check membership through MTProto; a missing username cannot be resolved safely."""
        if not username:
            return TelegramResult(TelegramResultKind.USERNAME_NOT_FOUND)
        try:
            channel = await self.client.get_input_entity(chat_id)
            user = await self.client.get_input_entity(f"@{username.lstrip('@')}")
            await self.client(
                functions.channels.GetParticipantRequest(channel=channel, participant=user)
            )
            return TelegramResult(TelegramResultKind.SUCCESS, value=True)
        except Exception as exc:
            return classify_error(exc)

    async def create_single_use_invite(self, chat_id: int, title: str) -> TelegramResult:
        try:
            channel = await self.client.get_input_entity(chat_id)
            invite = await self.client(
                functions.messages.ExportChatInviteRequest(
                    peer=channel, usage_limit=1, title=title[:32]
                )
            )
            return TelegramResult(TelegramResultKind.SUCCESS, value=invite.link)
        except Exception as exc:
            return classify_error(exc)

    async def revoke_invite(self, chat_id: int, link: str) -> TelegramResult:
        try:
            channel = await self.client.get_input_entity(chat_id)
            await self.client(
                functions.messages.EditExportedChatInviteRequest(
                    peer=channel, link=link, revoked=True
                )
            )
            return TelegramResult(TelegramResultKind.SUCCESS)
        except Exception as exc:
            return classify_error(exc)

    async def delete_supergroup(self, chat_id: int) -> TelegramResult:
        try:
            channel = await self.client.get_input_entity(chat_id)
            await self.client(functions.channels.DeleteChannelRequest(channel=channel))
            return TelegramResult(TelegramResultKind.SUCCESS)
        except Exception as exc:
            return classify_error(exc)
