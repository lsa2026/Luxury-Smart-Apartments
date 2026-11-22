"""Owner-selected manual WhatsApp handoff. No guest delivery is inferred."""

from django.conf import settings

from .telegram_stay_models import TelegramStayMessage, TelegramWorkgroupHandoff

WORKGROUP_ID = "-5525037012"


def enabled():
    return bool(getattr(settings, "TELEGRAM_WORKGROUP_HANDOFF_ENABLED", False))


def manual_mode():
    return enabled() or bool(getattr(settings, "TELEGRAM_WORKGROUP_ONLY_POLICY", True))


def message_model():
    return TelegramWorkgroupHandoff if manual_mode() else TelegramStayMessage


def is_handoff(row):
    return isinstance(row, TelegramWorkgroupHandoff)


def completed_state(row=None):
    return "handed_off" if (is_handoff(row) if row is not None else manual_mode()) else "accepted"


def fingerprint():
    from .telegram_stay_render import sha

    return sha("tg39:workgroup:" + WORKGROUP_ID)


def packet_fields():
    return (
        {"dispatch_mode": "workgroup_manual", "workgroup_id": WORKGROUP_ID} if manual_mode() else {}
    )


def canonical_phone(value):
    from apps.core.phone_numbers import normalize_phone_number

    try:
        return normalize_phone_number(value)
    except (ValueError, TypeError):
        if not manual_mode():
            raise
        return ""
