import os
import socket

from aiogram import Bot
from aiogram.client.session.aiohttp import AiohttpSession
from aiohttp.abc import AbstractResolver


class TelegramBotApiIPv4Resolver(AbstractResolver):
    """Keep Bot API reachable on hosts whose DNS has no usable IPv6 route."""

    async def resolve(
        self, host: str, port: int = 0, family: int = socket.AF_UNSPEC
    ) -> list[dict[str, object]]:
        if host != "api.telegram.org":
            raise OSError(f"Unexpected Bot API host: {host}")
        return [
            {
                "hostname": host,
                "host": os.environ.get("TELEGRAM_BOT_API_IPV4", "149.154.167.220"),
                "port": port,
                "family": socket.AF_INET,
                "proto": 0,
                "flags": socket.AI_NUMERICHOST,
            }
        ]

    async def close(self) -> None:
        return None


def build_telegram_bot(token: str) -> Bot:
    session = AiohttpSession()
    session._connector_init["resolver"] = TelegramBotApiIPv4Resolver()  # type: ignore[attr-defined]
    return Bot(token, session=session)
