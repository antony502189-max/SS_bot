import hashlib
import hmac
import json
from base64 import urlsafe_b64decode, urlsafe_b64encode
from datetime import UTC, datetime, timedelta

from fastapi import HTTPException


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
