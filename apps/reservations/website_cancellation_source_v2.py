"""Read the original confirmed cancellation journal; never cancel or refund."""

import json
from decimal import Decimal

from django.conf import settings
from django.db import transaction
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from django.views.decorators.debug import sensitive_variables

from apps.properties.models import Property


def admitted(event):
    from . import message_source_relay

    try:
        at = parse_datetime(getattr(settings, "MESSAGE_CANCELLATION_SOURCE_CUTOVER_AT", ""))
    except (TypeError, ValueError):
        return False
    created_at = (
        type(event).objects.filter(pk=event.pk).values_list("created_at", flat=True).first()
    )
    return bool(
        created_at
        and getattr(settings, "MESSAGE_CANCELLATION_SOURCE_V2_ENABLED", False)
        and getattr(settings, "MESSAGE_SOURCE_V2_ENABLED", False)
        and message_source_relay.enabled()
        and at
        and timezone.is_aware(at)
        and at <= created_at <= timezone.now()
        and message_source_relay.cutover() <= created_at
    )


@sensitive_variables()
def verified_fields(stay, event, scenario):
    from . import day_use_hostaway, morning_hostaway
    from . import message_source_authorization as source
    from .models import DayUseHostawayOwnership

    missing = {"final_cancellation_verified": None}
    if scenario not in source.SOURCES:
        return missing
    stays, events, _, _, comms, _ = source.SOURCES[scenario]
    if not isinstance(stay, stays) or not isinstance(event, events):
        return missing
    owner = morning_hostaway if scenario == "07" else day_use_hostaway
    with transaction.atomic():
        pointer = stays.objects.filter(pk=stay.pk).values_list("property_id", flat=True).first()
        if not pointer:
            return missing
        Property.objects.select_for_update().get(pk=pointer)
        current = (
            stays.objects.select_for_update(of=("self",)).select_related("property").get(pk=stay.pk)
        )
        stored = events.objects.select_for_update().filter(pk=event.pk, stay=current).first()
        if (
            not stored
            or current.property_id != pointer
            or current.environment != "production"
            or current.status != "cancelled"
            or not owner.hostaway_first(current)
            or not source.current_core(current, stored, comms)
            or stored.payload.get("event_type") != "cancelled"
        ):
            return missing
        operation = owner.booking_state(current)
        wanted = owner._request(current).payload
        provider_id = operation.get("reservation_id")
        if (
            operation.get("state") != "cancelled"
            or type(provider_id) is not int
            or provider_id <= 0
            or stored.payload.get("hostaway_reservation_id") != provider_id
            or operation.get("arrival_date") != wanted["arrivalDate"]
            or operation.get("departure_date") != wanted["departureDate"]
        ):
            return missing
        try:
            if Decimal(operation["amount_sar"]) != current.amount_sar:
                return missing
            for key in ("requested_at", "recorded_at"):
                timestamp = parse_datetime(operation[key])
                if (
                    not timestamp
                    or not timezone.is_aware(timestamp)
                    or timestamp > stored.created_at
                ):
                    return missing
        except (KeyError, ValueError, TypeError, ArithmeticError):
            return missing
        if (
            scenario == "08"
            and not DayUseHostawayOwnership.objects.filter(
                stay=current,
                state="cancelled",
                provider_reservation_id=provider_id,
                acknowledged_revision__gt=0,
            ).exists()
        ):
            return missing
        basis = {
            "scenario": scenario,
            "stay_id": str(current.pk),
            "event_id": str(stored.pk),
            "revision": str(stored.revision),
            "hostaway_reservation_id": str(provider_id),
            "state": "cancelled",
            "arrival_date": operation["arrival_date"],
            "departure_date": operation["departure_date"],
            "amount_sar": str(current.amount_sar),
        }
        reference = "site01-final-cancel-v1:" + source.sha(
            json.dumps(basis, sort_keys=True, separators=(",", ":"))
        )
        return {
            "final_cancellation_verified": True,
            "final_cancellation_evidence_ref": reference,
            "final_cancellation_source": {
                "kind": "original_confirmed_hostaway_cancellation_operation",
                "source_event_at": stored.created_at.isoformat(),
                "provider_confirmed_at": None,
                "evidence_ref": reference,
            },
        }
