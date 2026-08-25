import json
import logging
from datetime import UTC, datetime


class JsonFormatter(logging.Formatter):
    """Small dependency-free formatter compatible with container log collectors."""

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "timestamp": datetime.now(UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for key in ("request_id", "method", "path", "status_code", "duration_ms"):
            value = getattr(record, key, None)
            if value is not None:
                payload[key] = value
        return json.dumps(payload, default=str)


def configure_logging(level: str) -> None:
    root = logging.getLogger()
    if any(getattr(handler, "_ss_bot_json", False) for handler in root.handlers):
        return
    handler = logging.StreamHandler()
    handler._ss_bot_json = True  # type: ignore[attr-defined]
    handler.setFormatter(JsonFormatter())
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level.upper())
