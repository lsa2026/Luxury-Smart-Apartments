"""Small, central rules for the single-owner operations area."""

from __future__ import annotations

from django.conf import settings
from django.core.exceptions import PermissionDenied


def normalize_email(value: object) -> str:
    """Make email comparisons deterministic without displaying the value."""

    return str(value or "").strip().casefold()


def is_operations_owner(user: object) -> bool:
    """Return whether ``user`` may enter the private operations area.

    A successful social sign-in establishes identity only. The account still
    needs this exact business email and Django's two administrative flags.
    This deliberately keeps Google and Apple from being authority providers.
    """

    if not getattr(user, "is_authenticated", False):
        return False
    if not getattr(user, "is_active", False):
        return False
    if not (getattr(user, "is_staff", False) and getattr(user, "is_superuser", False)):
        return False
    if not settings.OPERATIONS_OWNER_ENFORCEMENT_ENABLED:
        return True
    return normalize_email(getattr(user, "email", "")) == settings.OPERATIONS_OWNER_EMAIL


def require_operations_owner(user: object) -> None:
    """Raise a normal Django permission denial for every non-owner account."""

    if not is_operations_owner(user):
        raise PermissionDenied
