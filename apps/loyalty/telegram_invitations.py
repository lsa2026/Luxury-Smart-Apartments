"""Binding gate for the existing invitation registry. Never sends or looks up receipts."""

from django.conf import settings
from django.core.exceptions import ValidationError

from apps.reservations.telegram_backend import Backend, Unavailable

from .invitation_registry import keyed


def target(reservation_key, phone):
    if not getattr(settings, "TELEGRAM_LOYALTY_INVITATIONS_ENABLED", False):
        raise ValidationError("telegram_invitation_disabled")
    if not reservation_key.startswith("hostaway:") or not phone:
        raise ValidationError("telegram_binding_required")
    client = None
    try:
        client = Backend()
        return client.binding(reservation_key[9:], phone)
    except Unavailable as exc:
        raise ValidationError("telegram_binding_unconfirmed") from exc
    finally:
        if client:
            client.close()


def digest(chat):
    return keyed("telegram:LuxurySmartApartmentsBot:" + chat)


def verify(row):
    chat = target(row.reservation_key, row.phone)
    if row.telegram_target_digest != digest(chat):
        raise ValidationError("telegram_binding_changed")
    return chat
