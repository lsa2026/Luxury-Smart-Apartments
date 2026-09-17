"""Keep the local customer profile aligned with allauth confirmation flows."""

from __future__ import annotations

from allauth.account.signals import email_confirmed
from django.dispatch import receiver

from .emails import queue_welcome_email
from .models import profile_for
from .services import claim_reservations_for_verified_email


@receiver(email_confirmed)
def sync_allauth_verified_email(sender, request, email_address, **kwargs):  # type: ignore[no-untyped-def]
    """Google and Apple confirmations unlock existing direct reservations."""
    del sender, kwargs
    profile = profile_for(email_address.user)
    if profile.is_email_verified:
        return
    profile.mark_verified()
    claim_reservations_for_verified_email(email_address.user)
    language = (getattr(request, "LANGUAGE_CODE", "") or "ar").split("-")[0]
    queue_welcome_email(email_address.user, language=language)
