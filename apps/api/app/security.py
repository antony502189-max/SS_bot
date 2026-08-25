import hashlib
import hmac
import json
from base64 import urlsafe_b64decode, urlsafe_b64encode
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qsl

from fastapi import HTTPException, status


def _b64encode(value: bytes) -> str:
    return urlsafe_b64encode(value).rstrip(b"=").decode()


def _b64decode(value: str) -> bytes:
    return urlsafe_b64decode(value + "=" * (-len(value) % 4))


def issue_access_token(user_id: str, secret: str, ttl_minutes: int) -> str:
    if not secret:
        raise RuntimeError("APP_SESSION_SECRET must be configured")
    payload = {
        "sub": user_id,
        "exp": int((datetime.now(UTC) + timedelta(minutes=ttl_minutes)).timestamp()),
    }
    encoded = _b64encode(json.dumps(payload, separators=(",", ":")).encode())
    signature = hmac.new(secret.encode(), encoded.encode(), hashlib.sha256).digest()
    return f"{encoded}.{_b64encode(signature)}"


def verify_access_token(token: str, secret: str) -> str:
    if not secret:
        raise HTTPException(status_code=503, detail="Application session signing is unavailable")
    try:
        encoded, signature = token.split(".", 1)
        expected = hmac.new(secret.encode(), encoded.encode(), hashlib.sha256).digest()
        if not hmac.compare_digest(expected, _b64decode(signature)):
            raise ValueError("signature")
        payload = json.loads(_b64decode(encoded))
        if not isinstance(payload["sub"], str) or int(payload["exp"]) <= int(
            datetime.now(UTC).timestamp()
        ):
            raise ValueError("expired")
        return payload["sub"]
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=401, detail="Session is invalid or expired") from exc


def verify_mini_app_init_data(init_data: str, bot_token: str) -> dict[str, str]:
    """Validate Telegram Web App initData using the official HMAC algorithm."""
    if not init_data or not bot_token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Telegram authentication unavailable"
        )
    values = dict(parse_qsl(init_data, keep_blank_values=True))
    received_hash = values.pop("hash", None)
    if not received_hash:
        raise HTTPException(status_code=401, detail="Telegram hash is missing")
    data_check_string = "\n".join(f"{key}={values[key]}" for key in sorted(values))
    secret = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    calculated = hmac.new(secret, data_check_string.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(calculated, received_hash):
        raise HTTPException(status_code=401, detail="Telegram signature is invalid")
    return values
