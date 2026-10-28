"""Central operations authorization: one owner and scoped Google managers."""

from __future__ import annotations

from functools import wraps

from django.conf import settings
from django.core.exceptions import PermissionDenied
from django.views.decorators.cache import never_cache


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


MANAGER_SESSION_KEY = "operations_google_manager"

# Explicit names, not a broad /admin/ prefix: managers never reach system tools.
MANAGER_VIEW_NAMES = frozenset(
    f"notifications:{name}"
    for name in (
        "hub",
        "booking_list",
        "booking_detail",
        "updated_payment_link_preview",
        "updated_payment_link_review",
        "morning_list",
        "morning_create",
        "morning_available_properties",
        "morning_detail",
        "booking_invoice",
        "manual_booking_list",
        "manual_booking_create",
        "manual_booking_detail",
        "manual_booking_available_properties",
        "manual_booking_disposal",
        "cancellation_list",
        "cancellation_detail",
        "refund_list",
        "refund_detail",
        "invoice_list",
        "invoice_detail",
        "invoice_issue",
        "invoice_pdf",
        "invoice_refresh_design",
        "invoice_send",
    )
) | {"admin_landing", "admin:login", "admin:logout"}


def has_operations_access(request) -> bool:
    """Check the current allowlist and a server-established Google session."""
    if is_operations_owner(request.user):
        return True
    user = request.user
    if not getattr(user, "is_authenticated", False) or not user.is_active:
        return False
    proof = request.session.get(MANAGER_SESSION_KEY)
    email = normalize_email(user.email)
    if (
        not isinstance(proof, dict)
        or proof.get("user_id") != user.pk
        or proof.get("email") != email
        or type(proof.get("grant_id")) is not int
        or type(proof.get("version")) is not int
    ):
        return False
    from .models import OperationsManager

    return OperationsManager.objects.filter(
        pk=proof.get("grant_id"),
        email=email,
        is_enabled=True,
        access_version=proof.get("version"),
    ).exists()


def require_operations_access(request) -> None:
    if not has_operations_access(request):
        raise PermissionDenied


def operations_access_required(view):
    """Custom operations access does not grant Django staff/superuser privileges."""

    @wraps(view)
    def wrapped(request, *args, **kwargs):
        if not request.user.is_authenticated:
            from django.contrib.auth.views import redirect_to_login
            from django.urls import reverse

            return redirect_to_login(request.get_full_path(), reverse("admin:login"))
        require_operations_access(request)
        return view(request, *args, **kwargs)

    return never_cache(wrapped)
