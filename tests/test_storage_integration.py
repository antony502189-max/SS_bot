import os
import uuid

import pytest

from apps.api.app.storage import delete_object, get_object_bytes, object_exists, put_object


@pytest.mark.skipif(
    os.getenv("RUN_MINIO_INTEGRATION") != "1",
    reason="set RUN_MINIO_INTEGRATION=1 with a disposable S3-compatible endpoint",
)
def test_s3_round_trip() -> None:
    key = f"integration/{uuid.uuid4()}.txt"
    payload = b"ss-bot-minio-round-trip"
    put_object(key, payload, "text/plain")
    try:
        assert object_exists(key)
        assert get_object_bytes(key) == payload
    finally:
        delete_object(key)
    assert not object_exists(key)
