"""Safe local property image upload handling."""

from pathlib import Path
from uuid import uuid4

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import UploadedFile
from PIL import Image, UnidentifiedImageError

SAFE_IMAGE_FORMATS = {"JPEG": ".jpg", "PNG": ".png", "WEBP": ".webp"}
SAFE_IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp"}


def property_image_upload_to(instance: object, filename: str) -> str:
    """Build a generated path; no user-controlled path segments are retained."""
    suffix = Path(filename).suffix.lower()
    safe_suffix = suffix if suffix in SAFE_IMAGE_SUFFIXES else ".img"
    property_id = getattr(instance, "property_id", None) or "unassigned"
    return f"properties/{property_id}/{uuid4().hex}{safe_suffix}"


def validate_property_image(upload: UploadedFile) -> None:
    """Validate content, size, dimensions, and format with Pillow."""
    if upload.size > settings.PROPERTY_IMAGE_MAX_BYTES:
        raise ValidationError("Image file exceeds the 10 MB size limit.")

    suffix = Path(upload.name).suffix.lower()
    if suffix not in SAFE_IMAGE_SUFFIXES:
        raise ValidationError("Only JPEG, PNG, and WebP images are allowed.")

    original_position = upload.tell()
    try:
        image = Image.open(upload)
        detected_format = image.format
        width, height = image.size
        image.verify()
    except (UnidentifiedImageError, OSError, SyntaxError) as exc:
        raise ValidationError("Uploaded file is not a valid image.") from exc
    finally:
        upload.seek(original_position)

    if detected_format not in SAFE_IMAGE_FORMATS:
        raise ValidationError("Only JPEG, PNG, and WebP images are allowed.")
    if width > settings.PROPERTY_IMAGE_MAX_WIDTH or height > settings.PROPERTY_IMAGE_MAX_HEIGHT:
        raise ValidationError("Image dimensions exceed the allowed limit.")
    if width * height > settings.PROPERTY_IMAGE_MAX_PIXELS:
        raise ValidationError("Image pixel count exceeds the allowed limit.")
