"""Model-owned07/08 snapshot. Sender and record release remain explicitly closed."""

import hashlib
import json
import uuid
from datetime import timedelta

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone
from django.views.decorators.debug import sensitive_variables

from apps.core.phone_numbers import InvalidPhoneNumber, normalize_phone_number
from apps.loyalty.invitation_auth import authenticated

from . import day_use_hostaway, morning_hostaway
from . import message_source_authorization as source
from .models import DayUseHostawayOwnership
from .telegram_authority import Denied, duplicate_safe, private_endpoint, private_response
from .telegram_backend import Unavailable
from .telegram_stay_journey import ReceiptPagePending

OWNERS = {"07": "morning07", "08": "dayuse08"}
FIELDS = {"contract", "scenario", "operation", "request_id", "event_id", "stay_key"}


def canonical_uuid(value):
    if not isinstance(value, str) or str(uuid.UUID(value)) != value:
        raise Denied
    return value


def resolve(data):
    if (
        not isinstance(data, dict)
        or set(data) != FIELDS
        or data["contract"] != "tg39_stay_source_request_v1"
        or data["scenario"] not in OWNERS
        or data["operation"] not in {"snapshot", "next", "review_next", "review_snapshot"}
    ):
        raise Denied
    canonical_uuid(data["request_id"])
    scenario = data["scenario"]
    model, events, pay_events, _, comms, payments = source.SOURCES[scenario]
    event = None
    financial = False
    stay_id = None
    if data["event_id"] is not None:
        event = events.objects.filter(pk=canonical_uuid(data["event_id"])).first()
        if event is None:
            event = pay_events.objects.filter(pk=data["event_id"]).first()
            financial = event is not None
        if event is None:
            raise Denied
        stay_id = event.stay_id
    if data["stay_key"] is not None:
        if not isinstance(data["stay_key"], str):
            raise Denied
        prefix, identifier = data["stay_key"].split(":", 1)
        if prefix != OWNERS[scenario]:
            raise Denied
        identifier = uuid.UUID(canonical_uuid(identifier))
        if stay_id is not None and stay_id != identifier:
            raise Denied
        stay_id = identifier
    if stay_id is None:
        raise Denied
    stay = model.objects.select_for_update(of=("self",)).select_related("property").get(pk=stay_id)
    event = event or events.objects.filter(stay=stay, revision=stay.communication_revision).first()
    if event:
        event.stay = stay
    current = (
        bool(event and payments.current(event))
        if financial
        else bool(event and source.current_core(stay, event, comms))
    )
    if financial:
        current = current and event.payload.get("payment_event_id") == str(event.pk)
        current = current and event.payload.get("payment_revision") == event.payment_revision
        current = current and event.payload.get("proof_key") == event.proof_key
    hostaway = morning_hostaway if scenario == "07" else day_use_hostaway
    operation = hostaway.booking_state(stay)
    provider_id = operation.get("reservation_id")
    if (
        stay.environment != "production"
        or not hostaway.hostaway_first(stay)
        or type(provider_id) is not int
        or provider_id <= 0
        or operation.get("state") not in {"confirmed", "cancelled"}
        or not comms.admitted(stay)
        or event is None
        or not current
        or (
            stay.ends_at <= timezone.now()
            and data["operation"] not in {"review_next", "review_snapshot"}
        )
        or (
            data["operation"] in {"review_next", "review_snapshot"}
            and stay.ends_at + timedelta(days=3) <= timezone.now()
        )
    ):
        raise Denied
    if scenario == "08":
        ownership = DayUseHostawayOwnership.objects.filter(stay=stay).first()
        if (
            ownership is None
            or ownership.provider_reservation_id != provider_id
            or ownership.state != operation["state"]
            or ownership.acknowledged_revision != ownership.revision
        ):
            raise Denied
        reference = "DU08:" + str(stay.pk)
    else:
        reference = "MorningStay " + str(stay.pk)  # Existing provider hostNote marker.
    # The dedicated source must not ambiguously own the same provider booking.
    other = source.SOURCES["08" if scenario == "07" else "07"][0]
    other_key = "hostaway_day_use" if scenario == "07" else "hostaway_morning"
    if other.objects.filter(
        environment="production",
        **{f"availability_evidence__{other_key}__reservation_id": provider_id},
    ).exists():
        raise Denied
    return scenario, stay, event, provider_id, reference, financial


def snapshot(data):
    scenario, stay, event, provider_id, reference, financial = resolve(data)
    now = timezone.now()
    phone = normalize_phone_number(stay.guest_phone)
    first, _, last = stay.guest_name.partition(" ")
    packet = {
        "contract": "tg39_stay_source_packet_v1",
        "scenario": scenario,
        "source_owner": OWNERS[scenario],
        "source_key": str(stay.pk),
        "stay_key": OWNERS[scenario] + ":" + str(stay.pk),
        "hostaway_reservation_id": str(provider_id),
        "ownership_reference": reference,
        "ownership_verified": True,
        "source_event_kind": "payment" if financial else "core",
        "event_id": str(event.pk),
        "source_revision": stay.communication_revision,
        "authoritative_at": now.isoformat(),
        "expires_at": (now + timedelta(seconds=30)).isoformat(),
        "decision": "snapshot" if data["operation"] in {"snapshot", "review_snapshot"} else "wait",
        "booking": {
            "phone_hash": hashlib.sha256(phone.lstrip("+").encode()).hexdigest(),
            "guest_name": stay.guest_name,
            "guest_first_name": first,
            "guest_last_name": last,
            "guest_email": stay.guest_email,
            "guest_language": stay.guest_language,
            "listing_map_id": stay.property.hostaway_listing_map_id,
            "listing_name": stay.property.name_ar or stay.property.name_en,
            "timezone": stay.time_zone,
            "starts_at": stay.starts_at.isoformat(),
            "ends_at": stay.ends_at.isoformat(),
            "booking_created_at": stay.created_at.isoformat(),
            "cancelled": stay.status == "cancelled",
        },
        # No provisional job or false success while render/binding/ledger are absent.
        "reason": "snapshot_only"
        if data["operation"] in {"snapshot", "review_snapshot"}
        else "stay_dispatch_not_ready",
    }
    if financial:
        packet["payment"] = {
            "payment_event_id": str(event.pk),
            "payment_revision": event.payment_revision,
            "proof_ref": event.proof_key,
            "paid_at": stay.paid_at.isoformat(),
            "status": "paid",
            "currency": "SAR",
            "amount_sar": f"{stay.amount_sar:.2f}",
            "booking_revision": stay.communication_revision,
        }
    return packet


@private_endpoint
@authenticated("POST")
@sensitive_variables()
def read_source(request):
    request._disable_google_integrations = True
    sending_enabled = getattr(settings, "TELEGRAM_STAY_MESSAGES_ENABLED", False)
    if not sending_enabled and not getattr(settings, "TELEGRAM_STAY_RECEIPTS_ENABLED", False):
        return private_response({"error": "stay_source_closed"}, 404)
    try:
        if request.GET or len(request.body) > 8192:
            raise Denied
        data = json.loads(request.body, object_pairs_hook=duplicate_safe)
        request._body = b""
        if isinstance(data, dict) and data.get("operation") in {"next", "review_next"}:
            # Historical receipt reconciliation precedes all current window and
            # revision checks. It never creates another provider attempt.
            if set(data) != FIELDS or data.get("contract") != "tg39_stay_source_request_v1":
                raise Denied
            canonical_uuid(data["request_id"])
            scenario = data["scenario"]
            prefix, identifier = data["stay_key"].split(":", 1)
            if scenario not in OWNERS or prefix != OWNERS[scenario]:
                raise Denied
            from .telegram_stay_journey import outstanding

            previous = outstanding(scenario, canonical_uuid(identifier))
            if previous:
                return private_response(previous)
        if not sending_enabled:
            return private_response({"error": "stay_source_closed"}, 404)
        with transaction.atomic():
            packet = snapshot(data)
        if data["operation"] not in {"snapshot", "review_snapshot"}:
            from .telegram_stay_journey import next_packet

            packet = next_packet(packet, data)
        return private_response(packet)
    except ReceiptPagePending:
        return private_response({"error": "receipt_scan_incomplete", "retry_only": True}, 503)
    except (
        Denied,
        ValueError,
        KeyError,
        TypeError,
        AttributeError,
        ValidationError,
        InvalidPhoneNumber,
        Unavailable,
        source.SOURCES["07"][0].DoesNotExist,
        source.SOURCES["08"][0].DoesNotExist,
    ):
        return private_response({"error": "stay_source_unverified"}, 409)
