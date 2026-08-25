import re
import uuid
from dataclasses import dataclass

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

from .config import get_settings

_SAFE_NAME = re.compile(r"[^A-Za-z0-9._-]")


@dataclass(frozen=True)
class ObjectMetadata:
    content_type: str
    size_bytes: int


def _client():
    settings = get_settings()
    if not settings.s3_endpoint_url:
        return None
    return boto3.client(
        "s3",
        endpoint_url=settings.s3_endpoint_url,
        aws_access_key_id=settings.s3_access_key,
        aws_secret_access_key=settings.s3_secret_key,
        region_name=settings.s3_region,
        config=Config(signature_version="s3v4"),
    )


def create_presigned_upload(
    task_id: uuid.UUID, filename: str, content_type: str
) -> tuple[str, str]:
    client = _client()
    if client is None:
        raise RuntimeError("Object storage is not configured")
    safe_name = _SAFE_NAME.sub("_", filename)
    object_key = f"tasks/{task_id}/{uuid.uuid4()}-{safe_name}"
    settings = get_settings()
    try:
        client.head_bucket(Bucket=settings.s3_bucket)
    except ClientError:
        client.create_bucket(Bucket=settings.s3_bucket)
    url = client.generate_presigned_url(
        "put_object",
        Params={"Bucket": settings.s3_bucket, "Key": object_key, "ContentType": content_type},
        ExpiresIn=settings.s3_presign_ttl_seconds,
        HttpMethod="PUT",
    )
    return object_key, url


def object_exists(object_key: str) -> bool:
    client = _client()
    if client is None:
        return False
    client.head_object(Bucket=get_settings().s3_bucket, Key=object_key)
    return True


def object_metadata(object_key: str) -> ObjectMetadata:
    client = _client()
    if client is None:
        raise RuntimeError("Object storage is not configured")
    response = client.head_object(Bucket=get_settings().s3_bucket, Key=object_key)
    return ObjectMetadata(
        content_type=str(response.get("ContentType") or ""),
        size_bytes=int(response["ContentLength"]),
    )


def get_object_bytes(object_key: str) -> bytes:
    client = _client()
    if client is None:
        raise RuntimeError("Object storage is not configured")
    response = client.get_object(Bucket=get_settings().s3_bucket, Key=object_key)
    return response["Body"].read()


def delete_object(object_key: str) -> None:
    client = _client()
    if client is None:
        raise RuntimeError("Object storage is not configured")
    client.delete_object(Bucket=get_settings().s3_bucket, Key=object_key)


def put_object(object_key: str, body: bytes, content_type: str) -> None:
    client = _client()
    if client is None:
        raise RuntimeError("Object storage is not configured")
    client.put_object(
        Bucket=get_settings().s3_bucket,
        Key=object_key,
        Body=body,
        ContentType=content_type,
    )
