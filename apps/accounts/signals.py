"""Keep the local customer profile aligned with allauth confirmation flows."""

from __future__ import annotations

from allauth.account.signals import email_confirmed
from django.contrib.auth.signals import user_logged_in
from django.dispatch import receiver
from django.utils import timezone

from .access import MANAGER_SESSION_KEY, normalize_email
from .emails import queue_welcome_email
from .models import OperationsManager, profile_for
from .services import claim_reservations_for_verified_email


@receiver(user_logged_in)
def record_guest_primary_auth(sender, request, user, **kwargs):
    from .phone_auth import PRIMARY_AUTH

    if request is None:
        return
    request.session["guest_identity_version"] = str(profile_for(user).auth_version)
    request.session.pop(PRIMARY_AUTH, None)
    if not getattr(request, "_guest_phone_authenticated", False):
        request.session[PRIMARY_AUTH] = {"user": user.pk, "at": int(timezone.now().timestamp())}


@receiver(email_confirmed)
def sync_allauth_verified_email(sender, request, email_address, **kwargs):  # type: ignore[no-untyped-def]
    """Google and Apple confirmations unlock existing direct reservations."""
    del sender, kwargs
    if normalize_email(email_address.email) != normalize_email(email_address.user.email):
        return
    profile = profile_for(email_address.user)
    if profile.is_email_verified:
        return
    profile.mark_verified()
    claim_reservations_for_verified_email(email_address.user)
    language = (getattr(request, "LANGUAGE_CODE", "") or "ar").split("-")[0]
    queue_welcome_email(email_address.user, language=language)


@receiver(user_logged_in)
def set_operations_google_session(sender, request, user, **kwargs):
    """Non-Google logins cannot inherit a previous manager authentication proof."""
    if request is None:
        return
    request.session.pop(MANAGER_SESSION_KEY, None)
    email = normalize_email(user.email)
    if getattr(request, "_operations_google_email", None) != email:
        return
    grant = OperationsManager.objects.filter(email=email, is_enabled=True).first()
    if grant is None or not user.is_active:
        return
    request.session[MANAGER_SESSION_KEY] = {
        "user_id": user.pk,
        "email": email,
        "grant_id": grant.pk,
        "version": grant.access_version,
    }
    OperationsManager.objects.filter(pk=grant.pk).update(last_google_login_at=timezone.now())
