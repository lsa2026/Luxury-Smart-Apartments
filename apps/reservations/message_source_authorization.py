"""Read-only facts for one signed source event. This is never send permission."""

import hashlib
import json
import re
from datetime import timedelta
from uuid import UUID
from zoneinfo import ZoneInfo

from django.conf import settings
from django.core import signing
from django.core.exceptions import ValidationError
from django.db import transaction
from django.http import JsonResponse
from django.utils import timezone
from django.utils.crypto import constant_time_compare, salted_hmac
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.debug import sensitive_variables

from apps.core.phone_numbers import InvalidPhoneNumber, normalize_phone_number

from . import (
    day_use08_communications,
    day_use08_payments,
    morning07_communications,
    morning07_payments,
)
from .models import (
    DayUse08DeliveryAttempt,
    DayUse08PaymentEvent,
    DayUseCommunicationEvent,
    DayUseStay,
    Morning07DeliveryAttempt,
    Morning07PaymentEvent,
    MorningCommunicationEvent,
    MorningStay,
)

SOURCES = {
    "07": (
        MorningStay,
        MorningCommunicationEvent,
        Morning07PaymentEvent,
        Morning07DeliveryAttempt,
        morning07_communications,
        morning07_payments,
    ),
    "08": (
        DayUseStay,
        DayUseCommunicationEvent,
        DayUse08PaymentEvent,
        DayUse08DeliveryAttempt,
        day_use08_communications,
        day_use08_payments,
    ),
}
HISTORY_LIMIT = 100
PURPOSES = {
    "status_new": "booking_confirmation",
    "status_modified": "booking_change",
    "status_cancelled": "booking_cancellation",
    "house_rules": "house_rules",
    "checkin_guide": "checkin_guide",
    "reminder_checkout": "checkout_reminder",
    "payment_success": "payment_confirmation",
    "loyalty_invitation": "loyalty_invitation",
    "payment_link": "payment_link",
}


def projected_consent(stay, scenario, purpose):
    # Retain unknown evidence; customer-service messaging does not require a
    # separate opt-in. Never fabricate a consent grant or a non-revocation.
    return dict.fromkeys(
        ("service", "marketing", "purpose", "evidence_ref", "revoked", "source_scope")
    )


def service_scope(facts):
    """Describe the verified business event, not permission or channel delivery."""
    kind = facts.get("source_message_type")
    if kind not in PURPOSES or facts.get("purpose") != PURPOSES[kind]:
        return None
    fields = (
        "reservation_key",
        "reservation_revision",
        "recipient_ref",
        "source_event_key",
        "source_message_type",
        "purpose",
    )
    if any(not isinstance(facts.get(k), str) or not facts[k] for k in fields):
        return None
    return {
        "contract_version": "lsa01-customer-service-scope-1",
        "business_identity": "Luxury Smart Apartments",
        "message_class": "customer_service",
        **{k: facts[k] for k in fields},
    }


def scoped_facts(facts):
    facts["service_scope"] = service_scope(facts)
    return facts


def sha(value):
    return hashlib.sha256(value.encode()).hexdigest()


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON key")
        result[key] = value
    return result


def response(body, status=200):
    result = JsonResponse(body, status=status)
    result["Cache-Control"] = "private, no-store, max-age=0"
    result["Referrer-Policy"] = "no-referrer"
    result["X-Robots-Tag"] = "noindex, nofollow"
    result["X-Content-Type-Options"] = "nosniff"
    result["Content-Security-Policy"] = "default-src 'none'; frame-ancestors 'none'"
    return result


def core_keys(stay_id, payload, scenario):
    prefix = f"lsa{scenario}:{stay_id}:"
    phone = re.sub(r"\D", "", payload["phone"])
    start = timezone.datetime.fromisoformat(payload["starts_at"])
    end = timezone.datetime.fromisoformat(payload["ends_at"])
    return {
        "status_" + payload["event_type"]: prefix + f"r{payload['source_revision']}:status",
        "house_rules": prefix + "rules:" + phone,
        "checkin_guide": prefix
        + f"guide:{payload['listing_map_id']}:"
        + str(int(start.timestamp() * 1000))
        + ":"
        + phone,
        "reminder_checkout": prefix
        + f"checkout:{payload['listing_map_id']}:"
        + str(int(end.timestamp() * 1000))
        + ":"
        + phone,
    }


def legacy_reference(key, scenario):
    return f"lsa{scenario}-" + hashlib.md5(key.encode(), usedforsecurity=False).hexdigest()


def legacy_state(states):
    # Provider acceptance is not delivery. Any ambiguous attempt prevents a repeat.
    if not states:
        return "not_attempted"
    for state in ("read", "delivered", "accepted", "posting", "uncertain"):
        if state in states:
            return state
    return "unknown"


def core_history(stay, purpose, scenario):
    from .message_receipt_models import WebsiteMessageExpectation

    events = list(stay.communication_events.order_by("revision")[: HISTORY_LIMIT + 1])
    if len(events) != stay.communication_revision or len(events) > HISTORY_LIMIT:
        return "unknown"
    refs = set()
    try:
        for revision, old in enumerate(events, 1):
            if old.revision != revision or old.payload["source_revision"] != revision:
                return "unknown"
            keys = core_keys(stay.pk, old.payload, scenario)
            # Booking confirmations are one semantic action even if a phone changes.
            # Modified/cancelled notices remain conservatively blocked by any prior
            # notice of that kind until a domain event ledger is reconciled.
            if purpose in keys:
                refs.add(legacy_reference(keys[purpose], scenario))
    except (KeyError, ValueError, TypeError):
        return "unknown"
    model = SOURCES[scenario][3]
    return legacy_state(
        list(model.objects.filter(stay=stay, pk__in=refs).values_list("state", flat=True))
        + list(
            WebsiteMessageExpectation.objects.filter(
                relay__stay_id=stay.pk,
                relay__scenario=scenario,
                relay__message_type=purpose,
                attempts_claimed__gt=0,
            ).values_list("state", flat=True)
        )
    )


def current_core(stay, event, comms):
    p = event.payload
    start = stay.starts_at.astimezone(ZoneInfo(stay.time_zone))
    end = stay.ends_at.astimezone(ZoneInfo(stay.time_zone))
    expected = {
        "source_revision": stay.communication_revision,
        "state_fingerprint": stay.communication_fingerprint,
        "source_event_id": str(event.pk),
        "phone": normalize_phone_number(stay.guest_phone),
        "listing_map_id": stay.property.hostaway_listing_map_id,
        "listing_name": stay.property.name_ar or stay.property.name_en,
        "starts_at": stay.starts_at.isoformat(),
        "ends_at": stay.ends_at.isoformat(),
        "timezone": stay.time_zone,
        "arrival_date": start.date().isoformat(),
        "departure_date": end.date().isoformat(),
        "check_in_time": start.strftime("%H:%M"),
        "check_out_time": end.strftime("%H:%M"),
        "event_type": "cancelled"
        if stay.status == "cancelled"
        else "new"
        if event.revision == 1
        else "modified",
        "guest_name": stay.guest_name,
        "guest_email": stay.guest_email,
        "guest_language": stay.guest_language,
        "guests": stay.guests,
        "total_price": str(stay.amount_sar),
        "currency": "SAR",
        "status": "confirmed"
        if comms.verified_booking(stay) and stay.status != "cancelled"
        else stay.status,
    }
    if comms.verified_booking(stay):
        from . import day_use_hostaway, morning_hostaway

        hostaway = morning_hostaway if isinstance(stay, MorningStay) else day_use_hostaway
        reservation_id = hostaway.booking_state(stay).get("reservation_id")
        if type(reservation_id) is not int or reservation_id <= 0:
            return False
        expected.update(
            contract_version=2, booking_verified=True, hostaway_reservation_id=reservation_id
        )
    return event.revision == stay.communication_revision and all(
        p.get(k) == v for k, v in expected.items()
    )


def base_facts(stay, event, scenario, locator, purpose, *, financial=False):
    phone = normalize_phone_number(stay.guest_phone)
    event_kind = "payment" if financial else "core"
    canonical_purpose = PURPOSES[purpose]
    consent = projected_consent(stay, scenario, canonical_purpose)
    result = {
        "contract_version": "yc19-source-facts-1",
        "source_family": "website",
        "scenario": scenario,
        "job_sha256": locator,
        "source_event_key": f"website:{scenario}:{event_kind}:{event.pk}",
        "source_occurred_at": event.created_at.isoformat(),
        "reservation_key": ("morning07:" if scenario == "07" else "dayuse08:") + str(stay.pk),
        "reservation_revision": str(stay.communication_revision),
        "recipient_e164": phone,
        "recipient_ref": "site01:"
        + salted_hmac(
            "website-source-recipient.v1", f"{scenario}:{stay.pk}:{phone}", algorithm="sha256"
        ).hexdigest(),
        "purpose": canonical_purpose,
        "source_message_type": purpose,
        # Exclude phone and routine source revision. A changed schedule must be
        # explicitly reconciled before the dispatcher authorises any new action.
        "semantic_key": sha(f"website:{scenario}:{stay.pk}:{purpose}"),
        "semantic_key_version": "website-message-slot-v1",
        "not_before": event.created_at.isoformat(),
        "expires_at": stay.ends_at.isoformat(),
        "environment": stay.environment,
        "language": stay.guest_language,
        "domain_authorized": False,
        "current_source_verified": True,
        "current_source_cancelled": stay.status == "cancelled",
        "final_cancellation_verified": None,
        "legacy_state": "unknown",
        "consent": consent,
        "human_takeover": None,
        "message": None,
        "render_policy_ref": None,
        "dispatch_allowed": False,
        "blockers": (["customer_messaging_withdrawn"] if consent["revoked"] is True else [])
        + ["render_policy_unverified", "human_takeover_requires_owner_ledger"],
    }
    from .message_receipts import projected_expectation

    result["receipt_expectation"] = projected_expectation(result)
    return result


def core_facts(stay, event, scenario, locator):
    keys = core_keys(stay.pk, event.payload, scenario)
    purpose = next(
        (kind for kind, key in keys.items() if constant_time_compare(sha(key), locator)), None
    )
    if not purpose:
        return payment_link_facts(stay, event, scenario, locator)
    if stay.status == "cancelled" and purpose != "status_cancelled":
        return None
    result = base_facts(stay, event, scenario, locator, purpose)
    from .message_receipts import predecessors_delivered

    predecessors_verified = predecessors_delivered(stay, event, scenario, purpose)
    result["domain_authorized"] = result["consent"]["revoked"] is not True and predecessors_verified
    if not predecessors_verified:
        result["blockers"].append("predecessor_delivery_unverified")
    if purpose == "status_cancelled":
        from .website_cancellation_source_v2 import admitted, verified_fields

        result.update(verified_fields(stay, event, scenario))
        if result["final_cancellation_verified"] is not True or not admitted(event):
            result["domain_authorized"] = False
            result["blockers"].append("confirmed_cancellation_source_and_cutover_required")
    result["legacy_state"] = core_history(stay, purpose, scenario)
    if purpose == "reminder_checkout":
        result["not_before"] = (stay.ends_at - timedelta(hours=1)).isoformat()
    allowed = (
        "guest_name",
        "guest_language",
        "listing_name",
        "listing_map_id",
        "starts_at",
        "ends_at",
        "timezone",
        "arrival_date",
        "departure_date",
        "check_in_time",
        "check_out_time",
        "guests",
        "total_price",
        "currency",
    )
    result["trusted_render_variables"] = {k: event.payload.get(k) for k in allowed}
    result["blockers"].append("core_template_and_access_data_owned_by_20")
    return scoped_facts(result)


def payment_link_facts(stay, event, scenario, locator):
    from . import day_use_payment_messages, morning_payment_messages

    links = morning_payment_messages if scenario == "07" else day_use_payment_messages
    rows = list(stay.payment_link_deliveries.order_by("created_at")[: HISTORY_LIMIT + 1])
    if len(rows) > HISTORY_LIMIT:
        return None
    prefix = "morning-pay-" if scenario == "07" else "day-use-pay-"
    row = next((r for r in rows if constant_time_compare(sha(prefix + str(r.pk)), locator)), None)
    if row is None or row.fingerprint != links.fingerprint(stay):
        return None
    if (
        stay.paid_at
        or stay.status not in {"open", "expired"}
        or stay.attempts.filter(
            status__in=["created", "pending", "unknown", "review", "succeeded"]
        ).exists()
    ):
        return None
    result = base_facts(stay, event, scenario, locator, "payment_link")
    result.update(
        source_event_key=f"website:{scenario}:payment_link:{row.pk}",
        source_occurred_at=row.created_at.isoformat(),
        not_before=row.created_at.isoformat(),
        legacy_state=legacy_state(
            [r.state for r in rows if r.state not in {"queued", "waiting_core"}]
        ),
    )
    # Never mint a payment URL, check live inventory, or claim an old queued row here.
    result["blockers"].extend(
        [
            "live_availability_required",
            "core_delivery_dependency_requires_verified_receipts",
            "payment_link_not_admitted_by_current_intent_contract",
        ]
    )
    return scoped_facts(result)


def payment_facts(stay, event, scenario, locator, payments):
    from .message_receipt_models import WebsiteMessageExpectation

    purpose = next(
        (
            kind
            for kind in payments.KINDS
            if constant_time_compare(sha(payments.reference(event.pk, kind)[0]), locator)
        ),
        None,
    )
    if not purpose:
        return None
    if purpose == "loyalty_invitation" and getattr(settings, "MESSAGE_SOURCE_V2_ENABLED", False):
        from .website_loyalty_source_v2 import source as loyalty_source

        return loyalty_source(event.pk, scenario)
    result = base_facts(stay, event, scenario, locator, purpose, financial=True)
    result.update(
        payment_revision=event.payment_revision,
        payment_verified=True,
        payment_status="paid",
        paid_at=stay.paid_at.isoformat(),
        financial_proof_ref=event.proof_key,
        source_recorded_at=event.created_at.isoformat(),
        source_occurred_at=stay.paid_at.isoformat(),
    )
    # Across all financial revisions of this stay, never just this event.
    deliveries = (
        payments.Morning07PaymentDelivery if scenario == "07" else payments.DayUse08PaymentDelivery
    )
    result["legacy_state"] = legacy_state(
        list(
            deliveries.objects.filter(event__stay=stay, message_type=purpose).values_list(
                "state", flat=True
            )
        )
        + list(
            WebsiteMessageExpectation.objects.filter(
                relay__stay_id=stay.pk,
                relay__scenario=scenario,
                relay__message_type=purpose,
                attempts_claimed__gt=0,
            ).values_list("state", flat=True)
        )
    )
    result["blockers"].append("core_delivery_dependency_requires_verified_receipts")
    if purpose == "payment_success":
        result["message"] = payments.render_success(stay)
        from .website_financial_source_v2 import verified_confirmation_fields

        if getattr(settings, "MESSAGE_SOURCE_V2_ENABLED", False):
            result.update(verified_confirmation_fields(event, scenario))
    else:
        from apps.loyalty import invitation_registry as registry

        key = (
            "morning07:" + str(stay.pk) if scenario == "07" else registry.day_use_booking_key(stay)
        )
        member = registry.member_exists(stay.guest_phone, stay.guest_email)
        prior = registry.competing(key, stay.guest_phone, stay.guest_email)
        result["loyalty_member_exists"] = member
        result["loyalty_invitation_recorded"] = bool(prior)
        if prior:
            result["legacy_state"] = legacy_state([prior.state])
        elif result["legacy_state"] == "not_attempted":
            # Older manual invitation receipts live in another domain. Do not
            # scan unrelated bookings or claim their absence as new eligibility.
            result["legacy_state"] = "unknown"
        result["blockers"].extend(
            [
                "payment_notice_delivery_unverified",
                "loyalty_registry_required",
            ]
        )
    return scoped_facts(result)


@csrf_exempt  # Authenticated by an existing per-event READ capability, never a cookie.
@sensitive_variables()
def source_authorization(request, *, scenario, financial=False):
    request._disable_google_integrations = True
    stays, events, pay_events, _attempts, comms, payments = SOURCES[scenario]
    header = (
        payments.HEADER
        if financial
        else f"X-LSA-{'Morning07' if scenario == '07' else 'DayUse08'}-Token"
    )
    # Do not leave the capability in Django's exception request metadata.
    capability = request.META.pop("HTTP_" + header.upper().replace("-", "_"), "")
    request.__dict__.pop("headers", None)
    if not getattr(settings, "MESSAGE_SOURCE_AUTH_ENABLED", False):
        return response({"code": "unavailable"}, 404)
    if request.method != "POST":
        return response({"code": "method_not_allowed"}, 405)
    if request.GET or request.content_type != "application/json" or len(request.body) > 1024:
        return response({"code": "invalid_request"}, 400)
    try:
        body = json.loads(request.body, object_pairs_hook=unique_object)
        if (
            not isinstance(body, dict)
            or set(body) != {"job_sha256"}
            or not isinstance(body["job_sha256"], str)
            or not re.fullmatch(r"[0-9a-f]{64}", body["job_sha256"])
        ):
            raise ValueError
        locator = body["job_sha256"]
    except (ValueError, TypeError):
        return response({"code": "invalid_request"}, 400)
    salt = payments.READ_SALT if financial else comms.SALT
    try:
        event_id = UUID(signing.TimestampSigner(salt=salt).unsign(capability, max_age=15 * 86400))
    except (signing.BadSignature, ValueError, TypeError):
        return response({"code": "unavailable"}, 404)
    enabled = (
        payments.enabled()
        if financial
        else (settings.MORNING07_N8N_ENABLED if scenario == "07" else settings.DAY_USE08_ENABLED)
    )
    if not enabled:
        return response({"code": "unavailable"}, 404)
    with transaction.atomic():
        model = pay_events if financial else events
        event = model.objects.filter(pk=event_id).first()
        if event is None:
            return response({"code": "unavailable"}, 404)
        from apps.properties.models import Property

        property_id = stays.objects.values_list("property_id", flat=True).get(pk=event.stay_id)
        Property.objects.select_for_update().get(pk=property_id)
        stay = (
            stays.objects.select_for_update(of=("self",))
            .select_related("property")
            .get(pk=event.stay_id)
        )
        event.stay = stay
        from .telegram_stay_transport import selected as telegram_selected

        if telegram_selected(stay):
            return response({"code": "telegram_owned_stay", "dispatch_allowed": False}, 409)
        if not isinstance(event.payload, dict):
            return response({"code": "unverified"}, 409)
        try:
            valid = payments.current(event) if financial else current_core(stay, event, comms)
            if financial:
                valid = valid and event.payload.get("payment_event_id") == str(event.pk)
                valid = valid and event.payload.get("payment_revision") == event.payment_revision
                valid = valid and event.payload.get("proof_key") == event.proof_key
            valid = valid and comms.admitted(stay) and stay.environment == "production"
            # Past stays, including old cancellation backlogs, are deliberately held.
            valid = valid and stay.ends_at > timezone.now()
            if not valid:
                return response({"code": "stale"}, 409)
            facts = (
                payment_facts(stay, event, scenario, locator, payments)
                if financial
                else core_facts(stay, event, scenario, locator)
            )
            if (
                financial
                and facts
                and facts["source_message_type"] == "payment_success"
                and getattr(settings, "MESSAGE_SOURCE_V2_ENABLED", False)
            ):
                from .website_financial_source_v2 import payment_confirmation_source

                facts = payment_confirmation_source(event.pk, scenario)
                if facts.get("available") is not True:
                    return response(
                        {"code": "financial_source_unverified", "dispatch_allowed": False}, 409
                    )
        except (InvalidPhoneNumber, ValidationError, KeyError, ValueError, TypeError):
            return response({"code": "unverified"}, 409)
        if facts is None:
            return response({"code": "job_not_in_scope"}, 404)
        from .transaction_permission_candidate import project

        facts.update(project(stay, scenario, facts, events))
        return response(facts)
