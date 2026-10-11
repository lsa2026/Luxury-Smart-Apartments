"""Request-level protection for the private Django administration surface."""

from __future__ import annotations

from collections.abc import Callable

from django.conf import settings
from django.core.exceptions import PermissionDenied
from django.http import HttpRequest, HttpResponse, HttpResponseNotAllowed

from .access import MANAGER_VIEW_NAMES, has_operations_access, is_operations_owner


class OwnerOnlyAdminMiddleware:
    """Keep system administration owner-only, with explicit manager routes.

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
            and not is_operations_owner(user)
        ):
            from django.urls import Resolver404, resolve

            try:
                name = resolve(request.path_info).view_name
            except Resolver404:
                name = ""
            # A signed-in guest may start the Google admin flow, but cannot
            # use its guest/email-code session to access any operational data.
            if name not in {"admin:login", "admin:logout"} and (
                name not in MANAGER_VIEW_NAMES or not has_operations_access(request)
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
