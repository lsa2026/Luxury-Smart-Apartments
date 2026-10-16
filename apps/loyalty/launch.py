"""Fail-closed live acceptance scope. No account, booking, or reward is created here."""

import re

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import validate_email


def pilot_only():
    return settings.LOYALTY_PILOT_ONLY


def pilot_emails():
    values = settings.LOYALTY_PILOT_EMAILS
    if not isinstance(values, list | tuple) or not 1 <= len(values) <= 20:
        return frozenset()
    try:
        normalized = frozenset(str(value).strip().casefold() for value in values)
        for value in normalized:
            validate_email(value)
        return normalized
    except ValidationError:
        return frozenset()


def pilot_references():
    values = settings.LOYALTY_PILOT_BOOKING_REFERENCES
    if not isinstance(values, list | tuple) or not 1 <= len(values) <= 20:
        return frozenset()
    references = frozenset(str(value).strip() for value in values)
    if any(not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,99}", v) for v in references):
        return frozenset()
    return references


def participant_allowed(user):
    if not pilot_only():
        return True
    return bool(
        user
        and getattr(user, "is_authenticated", False)
        and getattr(user, "is_active", False)
        and str(getattr(user, "email", "")).strip().casefold() in pilot_emails()
    )


def booking_allowed(booking):
    if not pilot_only():
        return True
    intent = booking.booking_intent if booking.booking_intent_id else None
    return bool(
        intent
        and intent.customer_id
        and participant_allowed(intent.customer)
        and booking.public_reference in pilot_references()
    )


def historical_award_allowed(booking):
    # This never changes original arrival/payment/consent timestamps, booking
    # price or cancellation policy. It is not a general retroactive launch.
    return bool(
        pilot_only() and settings.LOYALTY_PILOT_ALLOW_HISTORICAL and booking_allowed(booking)
    )
