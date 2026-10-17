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


class GuestIdentityLockMiddleware:
    """Close allauth's alternate email-management POST route for locked guests."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        return self.get_response(request)

    def process_view(self, request, view_func, view_args, view_kwargs):
        if request.user.is_authenticated and not request.user.is_staff:
            from .models import CustomerProfile

            profile = CustomerProfile.objects.filter(user=request.user).first()
            if (
                profile
                and profile.identity_changed_at
                and request.session.get("guest_identity_version") != str(profile.auth_version)
            ):
                from django.contrib.auth import logout
                from django.shortcuts import redirect

                logout(request)
                return redirect("accounts:phone_login")
        if request.method == "POST" and request.user.is_authenticated:
            from .identity import identity_locked

            if request.resolver_match.url_name == "account_email" and identity_locked(request.user):
                raise PermissionDenied("تغيير بريد العضوية من صلاحية الإدارة فقط.")
