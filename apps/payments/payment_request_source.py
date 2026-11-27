"""Original payment-delivery resources on the existing neutral carrier.

This module never collects, refunds, creates links, or posts to a provider.
Only fresh, pristine deliveries can be journalled after a coordinated cutover.
"""

import json
import re
from copy import copy
from datetime import timedelta
from uuid import UUID, uuid5

from django.conf import settings
from django.core import signing
from django.db import transaction
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.debug import sensitive_variables

from apps.core.phone_numbers import normalize_phone_number
from apps.properties.models import Property
from apps.reservations import message_source_authorization as source
from apps.reservations.message_relay_models import WebsiteMessageRelay
from apps.reservations.models import Reservation
from apps.reservations.website_template_selector_v2 import digest2

from . import payment_links
from .models import PaymentAttempt, PaymentRequestDelivery, ReservationPaymentRequest

FAMILY = "lsa01_payment_request"
SCENARIO = "01"
SALT = "payments.request-source-read.v1"
KINDS = {
    "link": ("payment_link", "payment_link"),
    "receipt": ("payment_success", "payment_confirmation"),
}
WINDOW = timedelta(hours=24)  # Message admission only; the payment URL is state gated.


def cutover():
    try:
        value = parse_datetime(getattr(settings, "MESSAGE_PAYMENT_REQUEST_SOURCE_CUTOVER_AT", ""))
    except (ValueError, TypeError):
        return None
    return value if value and timezone.is_aware(value) else None


def configured():
    return bool(getattr(settings, "MESSAGE_PAYMENT_REQUEST_SOURCE_V2_ENABLED", False) and cutover())


def enabled():
    from apps.reservations.message_source_relay import enabled as relay_enabled

    return (
        configured() and getattr(settings, "MESSAGE_SOURCE_V2_ENABLED", False) and relay_enabled()
    )


def delivery_uuid(delivery):
    return uuid5(
        delivery.request_id,
        f"payment-request-delivery:{delivery.pk}:{delivery.kind}:{delivery.sequence}",
    )


def job_sha(delivery):
    return source.sha(
        f"website:01:payment_request:{delivery_uuid(delivery)}:{delivery.request.environment}"
    )


def event_key(row):
    return "website:01:payment_request:" + str(row.event_id)


def routed(delivery_id):
    # The dated ownership boundary survives a pause; dispatch enable is separate.
    # Older resources retain their original channel, even while a future epoch is set.
    if WebsiteMessageRelay.objects.filter(payment_request_delivery_id=delivery_id).exists():
        return True
    at = cutover()
    return bool(
        at and PaymentRequestDelivery.objects.filter(pk=delivery_id, created_at__gte=at).exists()
    )


def active_for_new():
    return bool(enabled() and cutover() <= timezone.now())


def _locked(delivery_id):
    pointer = PaymentRequestDelivery.objects.values(
        "request_id", "request__reservation_id", "request__reservation__property_id"
    ).get(pk=delivery_id)
    Property.objects.select_for_update().get(pk=pointer["request__reservation__property_id"])
    reservation = (
        Reservation.objects.select_for_update(of=("self",))
        .select_related("booking_intent", "property")
        .get(pk=pointer["request__reservation_id"])
    )
    request = ReservationPaymentRequest.objects.select_for_update().get(
        pk=pointer["request_id"], reservation=reservation
    )
    request.reservation = reservation
    delivery = PaymentRequestDelivery.objects.select_for_update().get(
        pk=delivery_id, request=request
    )
    delivery.request = request
    return delivery, request, reservation


def _receipt_attempt(request, reservation):
    if (
        request.status != "paid"
        or not request.paid_at
        or reservation.payment_status.casefold() not in {"paid", "fully_paid", "partially_paid"}
    ):
        raise ValueError("verified_payment_required")
    attempts = list(PaymentAttempt.objects.select_for_update().filter(payment_request=request))
    successes = [a for a in attempts if a.status == "succeeded"]
    if len(successes) != 1 or any(
        a.status in {"refunded", "partially_refunded", "review", "unknown", "created", "pending"}
        for a in attempts
    ):
        raise ValueError("payment_requires_review")
    attempt = successes[0]
    if (
        attempt.provider != "hyperpay"
        or not attempt.provider_payment_id
        or not attempt.verified_at
        or attempt.verified_at > timezone.now()
        or attempt.booking_intent_id != reservation.booking_intent_id
        or (
            attempt.modification_request_id is not None
            and attempt.modification_request_id != request.modification_request_id
        )
        or attempt.amount != request.amount_sar
        or attempt.currency != "SAR"
    ):
        raise ValueError("payment_proof_mismatch")
    if (
        PaymentAttempt.objects.filter(
            booking_intent_id=reservation.booking_intent_id,
            status__in={
                "refunded",
                "partially_refunded",
                "review",
                "unknown",
                "created",
                "pending",
            },
        )
        .exclude(pk=attempt.pk)
        .exists()
    ):
        raise ValueError("competing_payment_requires_review")
    if (
        not request.modification_request_id
        and PaymentAttempt.objects.filter(
            booking_intent_id=reservation.booking_intent_id, status="succeeded"
        )
        .exclude(pk=attempt.pk)
        .exists()
    ):
        raise ValueError("competing_collection")
    return attempt


def _basis(delivery, request, reservation):
    intent = reservation.booking_intent
    if (
        delivery.kind not in KINDS
        or request.environment != "production"
        or settings.HYPERPAY_ENVIRONMENT != "production"
        or reservation.is_test
        or intent is None
        or not reservation.hostaway_reservation_id
        or reservation.normalized_status not in Reservation.ACTIVE_STATUSES
        or reservation.source_type
        not in {Reservation.SourceType.DIRECT_WEBSITE, Reservation.SourceType.HOSTAWAY_MANUAL}
        or not reservation.property_id
        or not reservation.property.hostaway_is_active
        or reservation.hostaway_listing_map_id != reservation.property.hostaway_listing_map_id
        or request.language not in {"ar", "en", "fr"}
        or intent.language != request.language
        or request.superseded_by_id
        or request.fingerprint != payment_links.fingerprint(reservation)
        or reservation.refund_obligations.exists()
        or reservation.modification_requests.filter(
            status__in={
                "requested",
                "pending_admin_approval",
                "ready_for_hostaway",
                "awaiting_payment",
                "processing",
                "unknown",
            }
        ).exists()
    ):
        raise ValueError("payment_source_changed")
    attempt = None
    if delivery.kind == "link":
        payment_links.require_enabled()
        payment_links.validate_open(request)
        if request.attempts.filter(
            status__in=(*payment_links.SETTLED, *payment_links.UNRESOLVED)
        ).exists():
            raise ValueError("payment_in_progress")
    else:
        attempt = _receipt_attempt(request, reservation)
        if request.modification_request_id:
            from .payment_link_revisions import validate_revision

            # The validator is read-only. Reuse its approved-delta checks after
            # collection without changing the stored paid request or its money.
            validation = copy(request)
            validation.status = "open"
            validate_revision(validation, reservation)
        elif (
            request.source_amount != reservation.total_price
            or request.source_currency != reservation.currency
            or request.amount_sar != intent.payment_amount_sar
            or reservation.modification_requests.filter(status="completed").exists()
        ):
            raise ValueError("payment_source_changed")
        from .currency import validate_payment_snapshot

        validate_payment_snapshot(
            source_amount=request.source_amount,
            source_currency=request.source_currency,
            payment_amount_sar=request.amount_sar,
            snapshot=request.settlement_snapshot["fx"]
            if request.modification_request_id
            else intent.exchange_rate_snapshot,
        )
    phone = normalize_phone_number(intent.guest_phone)
    recipient = (
        "site01:"
        + source.salted_hmac(
            "website-source-recipient.v1",
            f"payment_request:{reservation.pk}:{phone}",
            algorithm="sha256",
        ).hexdigest()
    )
    extension_basis = None
    from apps.reservations.stay_extension_models import StayExtension, StayExtensionChain
    from apps.reservations.stay_extensions import booking_verified, chain_context

    extension_pointer = (
        StayExtension.objects.filter(reservation=reservation)
        .values_list("chain_id", flat=True)
        .first()
    )
    if extension_pointer:
        chain = StayExtensionChain.objects.select_for_update().get(pk=extension_pointer)
        extension = (
            StayExtension.objects.select_for_update()
            .select_related("chain__property", "draft")
            .get(reservation=reservation)
        )
        extension.chain = chain
        extension.reservation = reservation
        context = chain_context(chain)
        if (
            not booking_verified(extension)
            or chain.review_required
            or context["chain_state"] == "review"
            or extension.state in {"cancelled", "review"}
        ):
            raise ValueError("extension_ownership_changed")
        terminal = context["terminal_hostaway_reservation_id"]
        if terminal not in {
            extension.previous_hostaway_reservation_id,
            reservation.hostaway_reservation_id,
        }:
            raise ValueError("extension_terminal_changed")
        extension_basis = {
            "id": str(extension.pk),
            "chain_id": str(chain.pk),
            "revision": chain.revision,
            "fingerprint": chain.fingerprint,
            "terminal": terminal,
            "ownership_acknowledged": extension.ownership_acknowledged,
            "state": extension.state,
        }
    binding = {
        "delivery_id": str(delivery.pk),
        "delivery_uuid": str(delivery_uuid(delivery)),
        "kind": delivery.kind,
        "sequence": delivery.sequence,
        "request_id": str(request.pk),
        "environment": request.environment,
        "financial_fingerprint": request.fingerprint,
        "amount": f"{request.amount_sar:.2f}",
        "currency": "SAR",
        "source_amount": str(request.source_amount),
        "source_currency": request.source_currency,
        "reservation_id": str(reservation.pk),
        "hostaway_id": str(reservation.hostaway_reservation_id),
        "arrival_date": reservation.check_in.isoformat(),
        "departure_date": reservation.check_out.isoformat(),
        "current_fingerprint": payment_links.fingerprint(reservation),
        "modification_id": str(request.modification_request_id)
        if request.modification_request_id
        else None,
        "approved_at": request.approved_at.isoformat() if request.approved_at else None,
        "settlement_sha256": digest2(request.settlement_snapshot),
        "extension": extension_basis,
        "recipient_ref": recipient,
        "recipient_sha256": source.sha(phone),
        "payment_attempt_id": str(attempt.pk) if attempt else None,
        "provider_id_sha256": source.sha(attempt.provider_payment_id) if attempt else None,
        "verified_at": attempt.verified_at.isoformat() if attempt else None,
        "paid_at": request.paid_at.isoformat() if request.paid_at else None,
    }
    name = f"{intent.guest_first_name} {intent.guest_last_name}".strip()
    listing = (
        getattr(reservation.property, "name_" + request.language, "")
        or reservation.property.name_en
    )
    if not name or not listing or request.amount_sar <= 0:
        raise ValueError("payment_source_incomplete")
    values = {
        "guest_name": name,
        "booking_reference": str(reservation.hostaway_reservation_id),
        "listing_name": listing,
        "amount": f"{request.amount_sar:.2f}",
        "currency": "SAR",
    }
    if delivery.kind == "link":
        values.update(
            arrival_date=reservation.check_in.isoformat(),
            departure_date=reservation.check_out.isoformat(),
            payment_url=payment_links.public_url(request),
        )
    binding["payment_url_sha256"] = (
        source.sha(values["payment_url"]) if delivery.kind == "link" else None
    )
    return binding, phone, values


def _pristine(delivery):
    from apps.reservations.telegram_stay_models import TelegramWebsiteHandoff

    return bool(
        delivery.status == "queued"
        and not delivery.provider_message_id
        and not delivery.sent_at
        and not PaymentRequestDelivery.objects.filter(request=delivery.request, kind=delivery.kind)
        .exclude(pk=delivery.pk)
        .exists()
        and not TelegramWebsiteHandoff.objects.filter(
            source_kind="payment_request", source_id=str(delivery.pk)
        ).exists()
    )


@sensitive_variables()
def register(delivery_id):
    with transaction.atomic():
        delivery, request, reservation = _locked(delivery_id)
        existing = WebsiteMessageRelay.objects.filter(payment_request_delivery=delivery).first()
        if existing:
            return {"status": "ycloud", "relay_ref": str(existing.pk)}
        at, now = cutover(), timezone.now()
        if (
            not enabled()
            or not at
            or not at <= delivery.created_at <= now < delivery.created_at + WINDOW
            or (delivery.kind == "receipt" and (not request.paid_at or request.paid_at < at))
            or not _pristine(delivery)
        ):
            return {"status": "held", "reason": "source_cutover_or_history"}
        try:
            binding, _, _ = _basis(delivery, request, reservation)
        except Exception:
            alert(delivery.pk, "current_payment_source_unverified")
            return {"status": "held", "reason": "current_payment_source_unverified"}
        kind, _ = KINDS[delivery.kind]
        row = WebsiteMessageRelay.objects.create(
            scenario=SCENARIO,
            financial=True,
            event_id=delivery_uuid(delivery),
            stay_id=reservation.pk,
            revision=request.fingerprint,
            message_type=kind,
            job_sha256=job_sha(delivery),
            source_occurred_at=delivery.created_at,
            not_before=delivery.created_at,
            expires_at=delivery.created_at + WINDOW,
            payment_request_delivery=delivery,
            source_binding_sha256=digest2(binding),
        )
        delivery.status, delivery.error_code = "ycloud", ""
        delivery.save(update_fields=["status", "error_code"])
        return {"status": "ycloud", "relay_ref": str(row.pk)}


def facts_for_row(row):
    delivery, request, reservation = _locked(row.payment_request_delivery_id)
    binding, phone, values = _basis(delivery, request, reservation)
    kind, purpose = KINDS[delivery.kind]
    if (
        row.scenario != SCENARIO
        or not row.financial
        or row.event_id != delivery_uuid(delivery)
        or row.stay_id != reservation.pk
        or row.revision != request.fingerprint
        or row.message_type != kind
        or row.job_sha256 != job_sha(delivery)
        or row.source_binding_sha256 != digest2(binding)
        or row.source_occurred_at != delivery.created_at
        or row.not_before != delivery.created_at
        or row.expires_at != delivery.created_at + WINDOW
        or row.expires_at <= timezone.now()
    ):
        raise ValueError("payment_journal_changed")
    current = (
        enabled()
        and cutover() <= delivery.created_at <= timezone.now() < row.expires_at
        and row.state in {"pending", "forwarding"}
        and delivery.status == "ycloud"
        and not delivery.provider_message_id
    )
    facts = {
        "contract_version": "yc19-source-facts-1",
        "available": True,
        "source_family": "website",
        "source_owner_family": "website",
        "scenario": SCENARIO,
        "job_sha256": row.job_sha256,
        "source_event_key": event_key(row),
        "source_event_id": str(row.event_id),
        "source_occurred_at": row.source_occurred_at.isoformat(),
        "reservation_key": str(reservation.hostaway_reservation_id),
        "reservation_revision": row.revision,
        "recipient_e164": phone,
        "recipient_ref": binding["recipient_ref"],
        "source_message_type": kind,
        "purpose": purpose,
        "language": request.language,
        "environment": "production",
        "not_before": row.not_before.isoformat(),
        "expires_at": row.expires_at.isoformat(),
        "semantic_key": source.sha(f"website:01:payment_request:{request.pk}:{delivery.kind}"),
        "semantic_key_version": "website-payment-request-slot-v1",
        "current_source_verified": True,
        "current_source_cancelled": False,
        "domain_authorized": bool(current),
        "legacy_state": "not_attempted",
        "consent": dict.fromkeys(("service", "marketing", "purpose", "evidence_ref", "revoked")),
        "human_takeover": None,
        "message": None,
        "render_policy_ref": None,
        "dispatch_allowed": False,
        "source_binding": binding,
        "source_binding_sha256": row.source_binding_sha256,
        "financial_conditions_verified": True,
        "variables": values,
        "blockers": ["human_takeover_requires_owner_ledger"],
    }
    context_keys = (
        "scenario",
        "job_sha256",
        "reservation_key",
        "reservation_revision",
        "recipient_ref",
        "source_event_key",
        "source_message_type",
        "purpose",
        "language",
        "not_before",
        "expires_at",
        "source_binding_sha256",
    )
    facts["template_context"] = {k: facts[k] for k in context_keys}
    facts["template_context"].update(
        source_family=FAMILY,
        source_context_ref="site01-payment-request-source-1:"
        + row.job_sha256
        + ":"
        + row.source_binding_sha256,
        recipient_binding_sha256=source.sha(facts["reservation_key"] + ":" + phone),
        preflight_status="ready",
        values=values,
    )
    from apps.reservations.message_receipts import projected_expectation

    facts["receipt_expectation"] = projected_expectation(facts)
    return delivery, source.scoped_facts(facts)


def alert(delivery_id, reason):
    from apps.notifications.services.email import queue_email

    safe = (
        reason
        if reason
        in {
            "current_payment_source_unverified",
            "source_changed_or_expired",
            "held",
            "uncertain",
            "rejected",
        }
        else "uncertain"
    )
    queue_email(
        message_type="integration_attention",
        recipient=settings.OPERATIONS_EMAIL,
        recipient_source="operations",
        recipient_reference=f"payment-request-source:{delivery_id}:{safe}",
        language="ar",
        idempotency_key="payment-request-source-attention:" + str(delivery_id),
    )


def read_capability(row):
    return signing.dumps(
        {
            "relay_ref": str(row.pk),
            "event_id": str(row.event_id),
            "job_sha256": row.job_sha256,
            "binding": row.source_binding_sha256,
        },
        salt=SALT,
        compress=False,
    )


@csrf_exempt
@sensitive_variables()
def authorization(request):
    request._disable_google_integrations = True
    capability = request.META.pop("HTTP_X_LSA_PAYMENT_REQUEST_TOKEN", "")
    request.__dict__.pop("headers", None)
    if not enabled():
        return source.response({"code": "unavailable"}, 404)
    if request.method != "POST":
        return source.response({"code": "method_not_allowed"}, 405)
    if request.GET or request.content_type != "application/json" or len(request.body) > 1024:
        return source.response({"code": "invalid_request"}, 400)
    try:
        body = json.loads(request.body, object_pairs_hook=source.unique_object)
        if (
            not isinstance(body, dict)
            or set(body) != {"job_sha256"}
            or not isinstance(body["job_sha256"], str)
            or not re.fullmatch(r"[a-f0-9]{64}", body["job_sha256"])
            or not isinstance(capability, str)
            or len(capability) > 2048
        ):
            raise ValueError
        claims = signing.loads(capability, salt=SALT, max_age=120)
        if (
            not isinstance(claims, dict)
            or set(claims) != {"relay_ref", "event_id", "job_sha256", "binding"}
            or claims["job_sha256"] != body["job_sha256"]
        ):
            raise ValueError
        with transaction.atomic():
            row = WebsiteMessageRelay.objects.select_for_update().get(
                pk=UUID(claims["relay_ref"]),
                scenario=SCENARIO,
                financial=True,
                event_id=UUID(claims["event_id"]),
                job_sha256=claims["job_sha256"],
                source_binding_sha256=claims["binding"],
            )
            _, facts = facts_for_row(row)
    except Exception:
        return source.response({"code": "unavailable", "dispatch_allowed": False}, 404)
    return source.response(facts)


def historical_scope(row):
    from apps.reservations.message_receipt_models import WebsiteMessageExpectation

    expectation = WebsiteMessageExpectation.objects.filter(relay=row).first()
    if expectation:
        if (
            digest2(expectation.source_binding) != row.source_binding_sha256
            or expectation.source_binding.get("delivery_uuid") != str(row.event_id)
            or expectation.source_binding.get("delivery_id") != str(row.payment_request_delivery_id)
        ):
            raise ValueError("payment_historical_binding_changed")
        return {
            key: expectation.identity[key]
            for key in (
                "scenario",
                "source_family",
                "job_sha256",
                "source_event_key",
                "reservation_key",
                "reservation_revision",
                "source_message_type",
                "purpose",
            )
        }
    delivery = PaymentRequestDelivery.objects.select_related("request__reservation").get(
        pk=row.payment_request_delivery_id
    )
    if (
        delivery_uuid(delivery) != row.event_id
        or delivery.request.reservation_id != row.stay_id
        or job_sha(delivery) != row.job_sha256
    ):
        raise ValueError("payment_historical_identity_changed")
    return {
        "scenario": SCENARIO,
        "source_family": FAMILY,
        "job_sha256": row.job_sha256,
        "source_event_key": event_key(row),
        "reservation_key": str(delivery.request.reservation.hostaway_reservation_id),
        "reservation_revision": row.revision,
        "source_message_type": row.message_type,
        "purpose": KINDS[delivery.kind][1],
    }


def initial_relay(proof):
    if proof.get("scenario") != SCENARIO or proof.get("source_family") != FAMILY:
        raise ValueError
    rows = list(
        WebsiteMessageRelay.objects.filter(
            scenario=SCENARIO,
            financial=True,
            state="forwarding",
            job_sha256=proof.get("job_sha256"),
            revision=proof.get("reservation_revision"),
            message_type=proof.get("source_message_type"),
            payment_request_delivery__isnull=False,
        )[:2]
    )
    if len(rows) != 1 or any(proof.get(k) != v for k, v in historical_scope(rows[0]).items()):
        raise ValueError
    return rows[0].pk


def policy_from_source(event, facts, context):
    from apps.reservations import website_template_selector_v2 as selector

    row = WebsiteMessageRelay.objects.get(payment_request_delivery=event, scenario=SCENARIO)
    _, current = facts_for_row(row)
    if (
        not current["domain_authorized"]
        or current != facts
        or not isinstance(context, dict)
        or context != current["template_context"]
    ):
        return None
    selected = selector.select_source_message({**current, "source_family": FAMILY}, context)
    return {
        "language": current["language"],
        "message": selected.message,
        "render_policy_ref": selected.selection_ref,
    }


def consume_ack(expectation):
    if expectation.relay.scenario != SCENARIO:
        return
    _, facts = facts_for_row(expectation.relay)
    if not facts["domain_authorized"] or expectation.identity.get("source_family") != FAMILY:
        raise ValueError("payment_source_ack_changed")
    expectation.source_binding = facts["source_binding"]
    expectation.save(update_fields=["source_binding"])


def project_receipt(expectation):
    if (
        expectation.relay.scenario != SCENARIO
        or expectation.identity.get("source_family") != FAMILY
    ):
        return
    row = expectation.relay
    if (
        expectation.source_binding.get("delivery_uuid") != str(row.event_id)
        or digest2(expectation.source_binding) != row.source_binding_sha256
        or expectation.attempts_claimed != 1
    ):
        raise ValueError("payment_receipt_binding_mismatch")
    delivery = PaymentRequestDelivery.objects.select_for_update().get(
        pk=row.payment_request_delivery_id
    )
    provider = expectation.provider_message_id
    if provider and (len(provider) > 255 or delivery.provider_message_id not in {"", provider}):
        raise ValueError("payment_receipt_provider_mismatch")
    rank = {"ycloud": 0, "uncertain": 0, "accepted": 1, "failed": 1, "delivered": 2, "read": 3}
    state = expectation.state
    if state not in rank or delivery.status not in rank or rank[state] < rank[delivery.status]:
        return
    delivery.status = state
    delivery.provider_message_id = provider or ""
    delivery.sent_at = delivery.sent_at or (timezone.now() if provider else None)
    delivery.save(update_fields=["status", "provider_message_id", "sent_at"])
    if state in {"failed", "uncertain"}:
        alert(delivery.pk, "rejected" if state == "failed" else "uncertain")
