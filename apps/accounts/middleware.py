"""Request-level protection for the private Django administration surface."""

from __future__ import annotations

from collections.abc import Callable

from django.conf import settings
from django.core.exceptions import PermissionDenied
from django.http import HttpRequest, HttpResponse, HttpResponseNotAllowed

from .access import is_operations_owner


class OwnerOnlyAdminMiddleware:
    """Allow only the designated business owner through ``/admin/``.

    Anonymous visitors are deliberately left to Django's normal login redirect.
    The check applies after authentication, which also covers custom internal
    screens living under the same URL prefix.
    """

    def __init__(self, get_response: Callable[[HttpRequest], HttpResponse]) -> None:
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponse:
        user = request.user
        if (
            settings.OPERATIONS_OWNER_ENFORCEMENT_ENABLED
            and request.path.startswith("/admin/")
            and getattr(user, "is_authenticated", False)
            and getattr(user, "is_staff", False)
            and not is_operations_owner(user)
        ):
            raise PermissionDenied
        return self.get_response(request)


class GoogleOnlyAdminLoginMiddleware:
    """Keep the private operations entrance limited to Google identity proof.

    Guest sign-in lives at ``/login/`` and ``/accounts/login/`` and is never
    inspected here.  This only rejects the legacy password POST endpoint used
    by Django's administration when Google sign-in has been enabled.
    """

    def __init__(self, get_response: Callable[[HttpRequest], HttpResponse]) -> None:
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponse:
        if (
            settings.GOOGLE_SIGN_IN_ENABLED
            and request.path == "/admin/login/"
            and request.method == "POST"
        ):
            return HttpResponseNotAllowed(["GET"])
        return self.get_response(request)
