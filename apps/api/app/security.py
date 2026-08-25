import hashlib
import hmac
from urllib.parse import parse_qsl

from fastapi import HTTPException, status


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
