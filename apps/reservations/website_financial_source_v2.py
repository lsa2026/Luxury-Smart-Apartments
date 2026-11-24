"""Closed financial source adapter, independent of sender input and old receipts.

Private READ routes invoke this module. Read locks and live inventory checks do not
claim a message, release checkout, change money, or authorize provider dispatch.
"""

from datetime import timedelta
from decimal import Decimal
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from django.conf import settings
from django.core import signing
from django.db import transaction
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.debug import sensitive_variables

from apps.core.phone_numbers import InvalidPhoneNumber, normalize_phone_number
from apps.properties.models import Property

from . import message_source_authorization as source


def hold(reason):
    return {"available": False, "reason": reason, "dispatch_allowed": False}


def modules(scenario):
    if scenario == "07":
        from . import morning_payment_messages as links
        from . import morning_stays as domain
    elif scenario == "08":
        from . import day_use_payment_messages as links
        from . import day_use_stays as domain
    else:
        raise ValueError("source_owner_mismatch")
    return links, domain


def _variables(stay, scenario, listing):
    if scenario not in source.SOURCES or not isinstance(stay, source.SOURCES[scenario][0]):
        raise ValueError
    amount = stay.amount_sar
    if (
        stay.guest_language not in {"ar", "en", "fr"}
        or not isinstance(amount, Decimal)
        or not amount.is_finite()
        or amount <= 0
        or amount != amount.quantize(Decimal("0.01"))
        or not timezone.is_aware(stay.starts_at)
        or not timezone.is_aware(stay.ends_at)
        or stay.starts_at >= stay.ends_at
    ):
        raise ValueError
    zone = ZoneInfo(stay.time_zone)
    start, end = (value.astimezone(zone) for value in (stay.starts_at, stay.ends_at))
    if not stay.guest_name or not listing:
        raise ValueError
    return {
        "guest_name": stay.guest_name,
        "listing_name": listing,
        "booking_reference": ("AM-" if scenario == "07" else "DU-") + str(stay.pk)[:8],
        "amount": f"{amount:.2f}",
        "currency": "SAR",  # This domain is explicitly SAR, never a conversion.
        "arrival_date": start.date().isoformat(),
        "departure_date": end.date().isoformat(),
        "check_in_time": start.strftime("%H:%M"),
        "check_out_time": end.strftime("%H:%M"),
        "timezone": stay.time_zone,
        # Canonical validated IANA name until an approved localized display exists.
        "timezone_display": stay.time_zone,
    }


def variables(stay, scenario):
    if scenario not in source.SOURCES or not isinstance(stay, source.SOURCES[scenario][0]):
        raise ValueError
    listing = getattr(stay.property, "name_" + stay.guest_language, "") or stay.property.name_en
    return _variables(stay, scenario, listing)


def core_variables(event, scenario):
    """Match source20's original core snapshot, without changing financial naming."""
    if scenario not in source.SOURCES or not isinstance(event, source.SOURCES[scenario][1]):
        raise ValueError
    event = type(event).objects.select_related("stay__property").get(pk=event.pk)
    if not source.current_core(event.stay, event, source.SOURCES[scenario][4]):
        raise ValueError
    return _variables(event.stay, scenario, event.payload["listing_name"])


def core_delivered(stay, event, scenario):
    """Only current bound delivery proofs/cache; never a provider lookup or ACK."""
    from .message_receipts import core_slot_delivered

    if not event or not source.current_core(stay, event, source.SOURCES[scenario][4]):
        return False
    return all(
        core_slot_delivered(stay, event, scenario, kind)
        for kind in source.core_keys(stay.pk, event.payload, scenario)
        if kind != "reminder_checkout"
    )


def snapshot(stay, event, scenario, kind, identity):
    phone = normalize_phone_number(stay.guest_phone)
    locator = (
        source.sha(source.SOURCES[scenario][5].reference(event.pk, kind)[0])
        if kind == "payment_success"
        else source.sha(
            ("morning-pay-" if scenario == "07" else "day-use-pay-") + identity["delivery_id"]
        )
    )
    base = source.base_facts(
        stay, event, scenario, locator, kind, financial=kind == "payment_success"
    )
    return {
        **base,
        "financial_source_contract": "site01-financial-source-2",
        "available": True,
        "scenario": scenario,
        "source_message_type": kind,
        "purpose": source.PURPOSES[kind],
        "reservation_key": f"{'morning07' if scenario == '07' else 'dayuse08'}:{stay.pk}",
        "reservation_revision": str(stay.communication_revision),
        "source_event_id": str(event.pk),
        "recipient_ref": "site01:"
        + source.salted_hmac(
            "website-source-recipient.v1", f"{scenario}:{stay.pk}:{phone}", algorithm="sha256"
        ).hexdigest(),
        "recipient_e164": phone,
        "language": stay.guest_language,
        "variables": variables(stay, scenario),
        "financial_identity": identity,
        "source_checked_at": timezone.now().isoformat(),
        "financial_conditions_verified": True,
        # Content/financial verification alone never overrides current channel ownership.
        "domain_authorized": False,
        "dispatch_allowed": False,
        "blockers": [*base["blockers"], "channel_cutover_required", "source_ack_v2_required"],
    }


def scoped_snapshot(result, stay):
    from .message_receipts import projected_expectation

    result["receipt_expectation"] = projected_expectation(result)
    if (
        result.get("financial_conditions_verified") is True
        and result.get("core_delivery_verified") is True
        and isinstance(result.get("variables"), dict)
    ):
        # Private READ wire contract consumed by19, not a caller-supplied render.
        # Build after final event/resource/URL fields are established; no raw phone
        # is copied into the template context and no capability is persisted.
        result["template_context"] = {
            key: result[key]
            for key in (
                "scenario",
                "source_family",
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
            )
        }
        result["template_context"].update(
            source_context_ref="site01-financial-source-2:" + result["job_sha256"],
            recipient_binding_sha256=source.sha(
                result["reservation_key"] + ":" + result["recipient_e164"]
            ),
            preflight_status="ready",
            values=dict(result["variables"]),
        )
    from . import message_source_relay as relay

    at = relay.cutover()
    # No caller/metadata boolean opens financial dispatch. A fresh source-owned
    # journal after coordinated cutover is required in addition to the domain checks.
    from .telegram_stay_transport import selected

    if (
        getattr(settings, "MESSAGE_SOURCE_V2_ENABLED", False)
        and relay.enabled()
        and at
        and not selected(stay)
        and result.get("core_delivery_verified") is True
        and result["consent"].get("revoked") is not True
    ):
        resource = result["financial_identity"]
        kind = result["source_message_type"]
        resource_id = resource["delivery_id" if kind == "payment_link" else "event_id"]
        created = timezone.datetime.fromisoformat(
            resource["created_at" if kind == "payment_link" else "recorded_at"]
        )
        admitted = relay.WebsiteMessageRelay.objects.filter(
            scenario=result["scenario"],
            financial=kind == "payment_success",
            event_id=resource_id,
            stay_id=result["reservation_key"].split(":", 1)[1],
            revision=result["reservation_revision"],
            message_type=kind,
            job_sha256=result["job_sha256"],
            source_occurred_at__gte=at,
            state__in=["pending", "forwarding"],
        ).exists()
        if (
            admitted
            and at <= created <= timezone.now()
            and result["legacy_state"] == "not_attempted"
        ):
            result["domain_authorized"] = True
            result["blockers"] = [
                b
                for b in result["blockers"]
                if b not in {"channel_cutover_required", "source_ack_v2_required"}
            ]
    return source.scoped_facts(result)


def verified_confirmation_fields(event, scenario):
    payments = source.SOURCES[scenario][5]
    if (
        not payments.current(event)
        or event.payload.get("payment_event_id") != str(event.pk)
        or event.payload.get("payment_revision") != event.payment_revision
        or event.payload.get("proof_key") != event.proof_key
    ):
        raise ValueError("current_collection_unverified")
    return {
        "variables": variables(event.stay, scenario),
        "financial_identity": {
            "resource_type": type(event).__name__,
            "event_id": str(event.pk),
            "payment_revision": event.payment_revision,
            "proof_key": event.proof_key,
            "proof_kind": event.proof_kind,
            "paid_at": event.stay.paid_at.isoformat(),
            "payment_status": "paid",
            "recorded_at": event.created_at.isoformat(),
        },
        "financial_conditions_verified": True,
    }


@sensitive_variables()
def payment_link_source(delivery_id, scenario):
    """Read a single existing delivery under original Property/stay/delivery locks.

    The URL is derived only by the existing source Signer, with no financial IO,
    clock extension, new delivery row or caller URL. Its resource is state-gated.
    """
    if scenario not in source.SOURCES:
        return hold("source_owner_mismatch")
    links, domain = modules(scenario)
    stays, events, *_ = source.SOURCES[scenario]
    try:
        delivery_id = UUID(str(delivery_id))
        model = stays._meta.get_field("payment_link_deliveries").related_model
        pointer = (
            model.objects.filter(pk=delivery_id).values("stay_id", "stay__property_id").first()
        )
        if not pointer:
            return hold("delivery_missing")
        with transaction.atomic():
            Property.objects.select_for_update().get(pk=pointer["stay__property_id"])
            stay = (
                stays.objects.select_for_update(of=("self",))
                .select_related("property")
                .get(pk=pointer["stay_id"])
            )
            row = model.objects.select_for_update().get(pk=delivery_id, stay=stay)
            if stay.property_id != pointer["stay__property_id"]:
                return hold("property_changed")
            event = events.objects.filter(stay=stay, revision=stay.communication_revision).first()
            comms = source.SOURCES[scenario][4]
            if (
                not settings.HYPERPAY_ENABLED
                or not (
                    settings.MORNING07_N8N_ENABLED
                    if scenario == "07"
                    else settings.DAY_USE08_ENABLED
                )
                or stay.environment != "production"
                or stay.environment != settings.HYPERPAY_ENVIRONMENT
                or stay.status not in {"open", "expired"}
                or stay.paid_at
                or stay.ends_at <= timezone.now()
                or row.state not in {"queued", "waiting_core"}
                or row.provider_id
                or row.completed_at
                or row.fingerprint != links.fingerprint(stay)
                or stay.attempts.filter(
                    status__in=["created", "pending", "unknown", "review", "succeeded"]
                ).exists()
                or not event
                or not source.current_core(stay, event, comms)
                or not comms.admitted(stay)
            ):
                return hold("current_unpaid_delivery_unverified")
            # No accepted, failed or ambiguous older send is adopted as fresh.
            if (
                stay.payment_link_deliveries.exclude(pk=row.pk)
                .exclude(state__in=["queued", "waiting_core", "expired"])
                .exists()
            ):
                return hold("prior_delivery_requires_reconciliation")
            from .message_receipt_models import WebsiteMessageExpectation

            if (
                WebsiteMessageExpectation.objects.filter(
                    relay__scenario=scenario,
                    relay__stay_id=stay.pk,
                    relay__message_type="payment_link",
                    attempts_claimed__gt=0,
                )
                .exclude(relay__event_id=row.pk)
                .exists()
            ):
                # A central ambiguous POST may have no provider ID and may not
                # yet be projected onto its delivery. Never conceal it with a new UUID.
                return hold("prior_delivery_requires_reconciliation")
            if not core_delivered(stay, event, scenario):
                # Keep a new source journal pending without forwarding or minting
                # a URL. A missing predecessor is not a stale financial request.
                key = ("morning-pay-" if scenario == "07" else "day-use-pay-") + str(row.pk)
                waiting = source.base_facts(stay, event, scenario, source.sha(key), "payment_link")
                waiting.update(
                    available=True,
                    booking_event_id=str(event.pk),
                    source_event_id=str(row.pk),
                    source_event_key=f"website:{scenario}:payment_link:{row.pk}",
                    source_occurred_at=row.created_at.isoformat(),
                    not_before=row.created_at.isoformat(),
                    financial_conditions_verified=False,
                    core_delivery_verified=False,
                    financial_identity={"delivery_id": str(row.pk), "fingerprint": row.fingerprint},
                )
                waiting["blockers"].append("core_delivery_unverified")
                return source.scoped_facts(waiting)
            domain.validate_window(
                stay.starts_at,
                stay.ends_at,
                zone=domain.property_zone(stay.property),
                allow_started=True,
                allow_elapsed=scenario == "07",
            )
            # Exactly the live source availability API; never caller/cached approval.
            domain.verify_window(
                stay.property,
                stay.starts_at,
                stay.ends_at,
                guests=stay.guests,
                allow_started=True,
                allow_elapsed=scenario == "07",
                linked_stay=stay,
            )
            if domain.local_inflight_conflict(stay.property, stay.starts_at, stay.ends_at) or (
                domain.active_stays(
                    stay.property,
                    stay.starts_at,
                    stay.ends_at + timedelta(hours=3),
                    exclude=stay.pk,
                ).exists()
            ):
                return hold("current_inventory_conflict")
            result = snapshot(
                stay,
                event,
                scenario,
                "payment_link",
                {
                    "resource_type": type(row).__name__,
                    "delivery_id": str(row.pk),
                    "fingerprint": row.fingerprint,
                    "created_at": row.created_at.isoformat(),
                    "payment_status": "unpaid",
                    "url_mode": "state_gated",
                    "link_expires_at": None,
                    "send_eligible_until": stay.ends_at.isoformat(),
                },
            )
            result["variables"]["payment_url"] = links.public_url(stay)
            result.update(
                source_family="website",
                booking_event_id=str(event.pk),
                source_event_id=str(row.pk),
                source_event_key=f"website:{scenario}:payment_link:{row.pk}",
                job_sha256=source.sha(
                    ("morning-pay-" if scenario == "07" else "day-use-pay-") + str(row.pk)
                ),
                source_occurred_at=row.created_at.isoformat(),
                not_before=row.created_at.isoformat(),
                expires_at=stay.ends_at.isoformat(),
                core_delivery_verified=True,
                legacy_state="not_attempted",
            )
            return scoped_snapshot(result, stay)
    except (ValueError, InvalidPhoneNumber, ZoneInfoNotFoundError):
        return hold("source_values_unverified")
    except (stays.DoesNotExist, Property.DoesNotExist):
        return hold("source_missing")
    except Exception:
        # Includes live inventory failures. Never expose operational data/errors.
        return hold("financial_source_unverified")


@sensitive_variables()
def payment_confirmation_source(event_id, scenario):
    if scenario not in source.SOURCES:
        return hold("source_owner_mismatch")
    stays, _, events, _, comms, payments = source.SOURCES[scenario]
    try:
        event_id = UUID(str(event_id))
        pointer = events.objects.filter(pk=event_id).values("stay_id", "stay__property_id").first()
        if not pointer:
            return hold("event_missing")
        with transaction.atomic():
            Property.objects.select_for_update().get(pk=pointer["stay__property_id"])
            stay = (
                stays.objects.select_for_update(of=("self",))
                .select_related("property")
                .get(pk=pointer["stay_id"])
            )
            event = events.objects.select_for_update().get(pk=event_id, stay=stay)
            event.stay = stay
            if (
                stay.property_id != pointer["stay__property_id"]
                or not payments.current(event)
                or not comms.admitted(stay)
                or event.payload.get("payment_event_id") != str(event.pk)
                or event.payload.get("payment_revision") != event.payment_revision
                or event.payload.get("proof_key") != event.proof_key
            ):
                return hold("current_collection_unverified")
            core = stay.communication_events.filter(revision=stay.communication_revision).first()
            core_verified = core_delivered(stay, core, scenario)
            result = snapshot(
                stay,
                event,
                scenario,
                "payment_success",
                {
                    "resource_type": type(event).__name__,
                    "event_id": str(event.pk),
                    "payment_revision": event.payment_revision,
                    "proof_key": event.proof_key,
                    "proof_kind": event.proof_kind,
                    "paid_at": stay.paid_at.isoformat(),
                    "payment_status": "paid",
                    "recorded_at": event.created_at.isoformat(),
                },
            )
            result.update(
                source_family="website",
                source_event_key=f"website:{scenario}:payment:{event.pk}",
                job_sha256=source.sha(payments.reference(event.pk, "payment_success")[0]),
                source_occurred_at=stay.paid_at.isoformat(),
                not_before=event.created_at.isoformat(),
                expires_at=stay.ends_at.isoformat(),
                legacy_state=source.payment_facts(
                    stay, event, scenario, result["job_sha256"], payments
                )["legacy_state"],
                core_delivery_verified=core_verified,
            )
            if not core_verified:
                # Projection remains useful for review; never a grant without core receipts.
                result["blockers"].append("core_delivery_unverified")
                return source.scoped_facts(result)
            return scoped_snapshot(result, stay)
    except Exception:
        return hold("current_collection_unverified")


@csrf_exempt  # Existing event READ signature, not a browser session or send token.
@sensitive_variables()
def payment_link_authorization(request, *, scenario):
    import json
    import re

    request._disable_google_integrations = True
    header = f"HTTP_X_LSA_{'MORNING07' if scenario == '07' else 'DAYUSE08'}_TOKEN"
    capability = request.META.pop(header, "")
    request.__dict__.pop("headers", None)
    if not getattr(settings, "MESSAGE_SOURCE_AUTH_ENABLED", False):
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
            or not re.fullmatch(r"[0-9a-f]{64}", body["job_sha256"])
        ):
            raise ValueError
    except (ValueError, TypeError):
        return source.response({"code": "invalid_request"}, 400)
    stays, events, _, _, comms, _ = source.SOURCES[scenario]
    try:
        event_id = UUID(
            signing.TimestampSigner(salt=comms.SALT).unsign(capability, max_age=15 * 86400)
        )
        event = events.objects.select_related("stay").get(pk=event_id)
    except (signing.BadSignature, ValueError, TypeError, events.DoesNotExist):
        return source.response({"code": "unavailable"}, 404)
    from .telegram_stay_transport import selected

    if selected(event.stay):
        return source.response({"code": "telegram_owned_stay", "dispatch_allowed": False}, 409)
    # Bound to one signed event and stay; no unscoped delivery or guest lookup.
    rows = list(
        event.stay.payment_link_deliveries.order_by("created_at")[: source.HISTORY_LIMIT + 1]
    )
    prefix = "morning-pay-" if scenario == "07" else "day-use-pay-"
    if len(rows) > source.HISTORY_LIMIT:
        return source.response({"code": "history_incomplete", "dispatch_allowed": False}, 409)
    row = next(
        (
            r
            for r in rows
            if source.constant_time_compare(source.sha(prefix + str(r.pk)), body["job_sha256"])
        ),
        None,
    )
    if not row:
        return source.response({"code": "job_not_in_scope"}, 404)
    facts = payment_link_source(row.pk, scenario)
    if not facts["available"]:
        return source.response({"code": facts["reason"], "dispatch_allowed": False}, 409)
    if facts["booking_event_id"] != str(event.pk):
        return source.response({"code": "stale", "dispatch_allowed": False}, 409)
    return source.response(facts)
