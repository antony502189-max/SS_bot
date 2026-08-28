import json
import logging
import re
from datetime import UTC, datetime

_REDACTIONS = (
    (re.compile(r"\b\d{6,12}:[A-Za-z0-9_-]{20,}\b"), "[REDACTED_BOT_TOKEN]"),
    (
        re.compile(r"https?://(?:t\.me|telegram\.me)/(?:\+|joinchat/)[^\s]+", re.IGNORECASE),
        "[REDACTED_TELEGRAM_INVITE]",
    ),
    (
        re.compile(
            r"(?i)\b(token|secret|password|api_hash|access_key)=([^\s&]+)",
        ),
        r"\1=[REDACTED]",
    ),
)


def redact_log_text(value: object) -> str:
    text = str(value)
    for pattern, replacement in _REDACTIONS:
        text = pattern.sub(replacement, text)
    return text


class JsonFormatter(logging.Formatter):
    """Small dependency-free formatter compatible with container log collectors."""

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "timestamp": datetime.now(UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": redact_log_text(record.getMessage()),
        }
        for key in (
            "request_id",
            "method",
            "path",
            "status_code",
            "duration_ms",
            "operation",
            "event_type",
            "outbox_event_id",
            "attempt",
            "error_type",
        ):
            value = getattr(record, key, None)
            if value is not None:
                payload[key] = redact_log_text(value) if isinstance(value, str) else value
        if record.exc_info:
            payload["exception"] = redact_log_text(self.formatException(record.exc_info))
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
