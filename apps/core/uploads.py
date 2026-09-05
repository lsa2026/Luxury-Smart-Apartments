"""Safe upload paths and validation for administrator-managed interface images."""

from pathlib import Path
from uuid import uuid4

from django.core.files.uploadedfile import UploadedFile

from apps.properties.uploads import validate_property_image

SAFE_IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp"}


def site_interface_image_upload_to(instance: object, filename: str) -> str:
    """Generate a stable, non-user-controlled path for an interface image."""
    suffix = Path(filename).suffix.lower()
    safe_suffix = suffix if suffix in SAFE_IMAGE_SUFFIXES else ".img"
    placement = getattr(instance, "placement", None) or "unassigned"
    return f"site-interface/{placement}/{uuid4().hex}{safe_suffix}"


def validate_site_interface_image(upload: UploadedFile) -> None:
    """Apply the same hardened image contract used for property uploads."""
    validate_property_image(upload)
