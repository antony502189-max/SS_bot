from dataclasses import dataclass
from io import BytesIO

from PIL import Image, UnidentifiedImageError


@dataclass(frozen=True)
class ProcessedPhoto:
    content_type: str
    width: int
    height: int
    preview_bytes: bytes


def inspect_photo(raw: bytes) -> ProcessedPhoto:
    """Decode persisted bytes and create a bounded JPEG preview."""
    try:
        with Image.open(BytesIO(raw)) as image:
            image.verify()
        with Image.open(BytesIO(raw)) as image:
            if image.format not in {"JPEG", "PNG", "WEBP"}:
                raise ValueError("unsupported_image_format")
            content_type = {
                "JPEG": "image/jpeg",
                "PNG": "image/png",
                "WEBP": "image/webp",
            }[image.format]
            width, height = image.size
            preview = image.convert("RGB")
            preview.thumbnail((960, 960))
            output = BytesIO()
            preview.save(output, format="JPEG", quality=82, optimize=True)
    except (UnidentifiedImageError, OSError) as exc:
        raise ValueError("invalid_image") from exc
    return ProcessedPhoto(content_type, width, height, output.getvalue())
