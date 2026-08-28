"""Telegram bot runtime bootstrap.

Handler definitions live outside the entry point so runtime orchestration is not
mixed into the public bootstrap. Re-exporting public names preserves imports used
by existing integrations while handler groups continue to be split incrementally.
"""

import asyncio

from .handlers.core import *  # noqa: F403 - compatibility for existing bot integrations
from .handlers.core import run

if __name__ == "__main__":
    asyncio.run(run())
