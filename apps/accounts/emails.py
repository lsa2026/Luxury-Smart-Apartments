"""Queueing the account emails through the project's delivery pipeline.

Django's own password-reset view sends mail directly, which would bypass the
retry, idempotency, masking and audit trail every other message on this site
goes through. These helpers put account mail on the same queue instead.
"""

import secrets

from django.contrib.auth.models import AbstractBaseUser
from django.utils import timezone

from apps.notifications.models import EmailDelivery
from apps.notifications.services.email import queue_email


def _language(language: str) -> str:
    return language if language in {"ar", "en", "fr"} else "ar"


def queue_verification_email(user: AbstractBaseUser, *, language: str = "ar") -> EmailDelivery:
    """Ask the customer to confirm the address on the account.

    The idempotency key carries a nonce so a customer who asks again really does
    get another message; abuse is held back by the rate limit on the view, not by
    silently collapsing repeat requests into one.
    """
    return queue_email(
        message_type="account_verify_email",
        recipient=user.email,
        recipient_source="account",
        recipient_reference=str(user.pk),
        language=_language(language),
        idempotency_key=f"account-verify:{user.pk}:{secrets.token_urlsafe(12)}",
    )


def queue_password_reset_email(user: AbstractBaseUser, *, language: str = "ar") -> EmailDelivery:
    return queue_email(
        message_type="account_password_reset",
        recipient=user.email,
        recipient_source="account",
        recipient_reference=str(user.pk),
        language=_language(language),
        idempotency_key=f"account-reset:{user.pk}:{secrets.token_urlsafe(12)}",
    )


def queue_welcome_email(user: AbstractBaseUser, *, language: str = "ar") -> EmailDelivery:
    """Sent once, after the address is proven; keyed so a replay cannot repeat it."""
    stamp = timezone.now().strftime("%Y%m%d")
    return queue_email(
        message_type="account_welcome",
        recipient=user.email,
        recipient_source="account",
        recipient_reference=str(user.pk),
        language=_language(language),
        idempotency_key=f"account-welcome:{user.pk}:{stamp}",
    )
