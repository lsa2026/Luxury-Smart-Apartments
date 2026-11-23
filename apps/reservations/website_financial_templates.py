"""Read-only financial template candidate. No route, ACK, release or provider call.

The caller must already own the scoped source operation. These helpers prove
content only; domain/consent/media/provider approval remain separate gates.
"""

import hashlib
import json
import re
from decimal import Decimal
from importlib.resources import files
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from django.conf import settings
from django.core import signing
from django.urls import resolve
from django.urls.exceptions import Resolver404
from django.utils import timezone
from django.views.decorators.debug import sensitive_variables

from . import message_source_authorization as source

REGISTRY_SHA256 = "762e59f7d67b8b53b061f0c5e5fca61232df38f2ae31c9b558fc8038e7590eb6"
SUCCESS_KEYS = (
    "guest_name",
    "listing_name",
    "starts_at_local",
    "ends_at_local",
    "amount_sar",
    "booking_reference",
)
LINK_KEYS = (
    "guest_name",
    "listing_name",
    "booking_reference",
    "stay_date",
    "check_in_time",
    "check_out_time",
    "timezone",
    "amount_sar",
    "payment_url",
)


def hold(reason):
    return {"available": False, "reason": reason, "dispatch_allowed": False}


def source_variables(stay, scenario):
    if scenario not in source.SOURCES or not isinstance(stay, source.SOURCES[scenario][0]):
        raise ValueError("source_owner_mismatch")
    if stay.guest_language not in {"ar", "en", "fr"} or stay.environment != "production":
        raise ValueError("source_language_or_environment")
    amount = stay.amount_sar
    if (
        not isinstance(amount, Decimal)
        or not amount.is_finite()
        or amount <= 0
        or amount != amount.quantize(Decimal("0.01"))
        or not timezone.is_aware(stay.starts_at)
        or not timezone.is_aware(stay.ends_at)
        or stay.starts_at >= stay.ends_at
    ):
        raise ValueError("source_amount_or_period")
    zone = ZoneInfo(stay.time_zone)
    start, end = (x.astimezone(zone) for x in (stay.starts_at, stay.ends_at))
    listing = getattr(stay.property, "name_" + stay.guest_language, "") or stay.property.name_en
    if not stay.guest_name or not listing:
        raise ValueError("source_labels_missing")
    return {
        "guest_name": stay.guest_name,
        "listing_name": listing,
        "starts_at_local": start.strftime("%Y-%m-%d %H:%M"),
        "ends_at_local": end.strftime("%Y-%m-%d %H:%M"),
        "stay_date": start.strftime("%Y-%m-%d"),
        "check_in_time": start.strftime("%H:%M"),
        "check_out_time": end.strftime("%H:%M"),
        "timezone": stay.time_zone,
        "amount_sar": f"{amount:.2f}",
        "booking_reference": ("AM-" if scenario == "07" else "DU-") + str(stay.pk)[:8],
    }


def _projection(stay, scenario, kind, variables, identity):
    return {
        "contract": "site01-financial-projection-1",
        "available": True,
        "scenario": scenario,
        "source_message_type": kind,
        "purpose": source.PURPOSES[kind],
        "language": stay.guest_language,
        "reservation_key": ("morning07:" if scenario == "07" else "dayuse08:") + str(stay.pk),
        "reservation_revision": str(stay.communication_revision),
        "currency": "SAR",
        "variables": variables,
        "identity": identity,
        "domain_authorized": False,
        "dispatch_allowed": False,
    }


@sensitive_variables()
def payment_success_projection(event, scenario):
    if scenario not in source.SOURCES or not isinstance(event, source.SOURCES[scenario][2]):
        return hold("source_owner_mismatch")
    event = type(event).objects.select_related("stay__property").get(pk=event.pk)
    payments = source.SOURCES[scenario][5]
    if (
        not payments.current(event)
        or event.payload.get("payment_event_id") != str(event.pk)
        or event.payload.get("payment_revision") != event.payment_revision
        or event.payload.get("proof_key") != event.proof_key
    ):
        return hold("current_collection_unverified")
    values = source_variables(event.stay, scenario)
    result = _projection(
        event.stay,
        scenario,
        "payment_success",
        values,
        {
            "source_event_id": str(event.pk),
            "payment_revision": event.payment_revision,
            "financial_proof_ref": event.proof_key,
            "paid_at": event.stay.paid_at.isoformat(),
        },
    )
    result["blockers"] = ["financial_domain_and_core_receipts_not_authorized"]
    return result


@sensitive_variables()
def _existing_url(stay, scenario, value):
    """Validate a supplied existing signed resource; never mint/refresh one."""
    if not isinstance(value, str) or not value or len(value) > 4096:
        return None
    parsed, base = urlsplit(value), urlsplit(settings.SITE_BASE_URL)
    if (
        parsed.scheme != "https"
        or parsed.netloc != base.netloc
        or base.scheme != "https"
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        return None
    try:
        route = resolve(parsed.path)
        expected = "morning_payment" if scenario == "07" else "day_use_payment"
        if route.view_name != "payments:" + expected or set(route.kwargs) != {"token"}:
            return None
        salt = "payments.morning-stay.v1" if scenario == "07" else "payments.day_use-stay.v1"
        data = signing.Signer(salt=salt).unsign_object(route.kwargs["token"])
        return value if data == {"stay": str(stay.pk)} else None
    except (signing.BadSignature, ValueError, TypeError, Resolver404):
        return None


@sensitive_variables()
def payment_link_projection(stay, delivery, scenario, *, existing_url=None):
    from . import day_use_payment_messages, morning_payment_messages

    if scenario not in source.SOURCES or not isinstance(stay, source.SOURCES[scenario][0]):
        return hold("source_owner_mismatch")
    stay = type(stay).objects.select_related("property").get(pk=stay.pk)
    links = morning_payment_messages if scenario == "07" else day_use_payment_messages
    delivery = stay.payment_link_deliveries.filter(pk=delivery.pk).first()
    if (
        not delivery
        or delivery.state not in {"queued", "waiting_core"}
        or delivery.fingerprint != links.fingerprint(stay)
        or stay.status not in {"open", "expired"}
        or stay.paid_at
        or stay.ends_at <= timezone.now()
        or stay.attempts.filter(
            status__in=["created", "pending", "unknown", "review", "succeeded"]
        ).exists()
    ):
        return hold("current_unpaid_delivery_unverified")
    payment_url = _existing_url(stay, scenario, existing_url)
    if payment_url is None:
        return hold("existing_payment_url_not_bound")
    values = {**source_variables(stay, scenario), "payment_url": payment_url}
    result = _projection(
        stay,
        scenario,
        "payment_link",
        values,
        {
            "source_delivery_id": str(delivery.pk),
            "fingerprint": delivery.fingerprint,
            "source_created_at": delivery.created_at.isoformat(),
            "send_window_ends_at": stay.ends_at.isoformat(),
            "url_has_clock_expiry": False,
        },
    )
    result["blockers"] = [
        "live_availability_required",
        "verified_core_delivery_required",
        "payment_link_source_ack_and_intent_contract_required",
    ]
    return result


def independent_template_policy(projection):
    """Internal projection only. This result is not an authority/receipt ACK."""
    if projection.get("available") is not True or projection.get("dispatch_allowed") is not False:
        return hold("source_projection_unavailable")
    raw = (
        files("apps.reservations")
        .joinpath("morning_assets", "financial-template-bindings.json")
        .read_bytes()
    ).replace(b"\r\n", b"\n")  # Git text checkout endings do not alter the pinned catalog.
    if hashlib.sha256(raw).hexdigest() != REGISTRY_SHA256:
        return hold("template_catalog_integrity_failed")
    pack = json.loads(raw)
    rows = [
        r
        for r in pack["variants"]
        if (
            r["scenario"] == projection["scenario"]
            and r["language"] == projection["language"]
            and r["source_message_type"] == projection["source_message_type"]
        )
    ]
    if len(rows) != 1:
        return hold("template_binding_missing_or_ambiguous")
    binding = rows[0]
    expected = SUCCESS_KEYS if projection["source_message_type"] == "payment_success" else LINK_KEYS
    if (
        binding["ordered_source_keys"] != list(expected)
        or binding["purpose"] != projection["purpose"]
        or source.sha(binding["body"]) != binding["body_sha256"]
        or sorted({int(x) for x in re.findall(r"\{\{(\d+)\}\}", binding["body"])})
        != list(range(1, len(expected) + 1))
    ):
        return hold("template_binding_integrity_failed")
    ordered = [projection["variables"].get(k) for k in expected]
    if any(not isinstance(v, str) or not v or len(v) > 4096 for v in ordered):
        return hold("source_variable_missing")
    message = {"mode": "template", "template_ref": binding["name"], "variables": ordered}
    return {
        "available": True,
        "language": binding["language"],
        "message": message,
        "content_digest": source.sha(
            json.dumps([binding["language"], message], ensure_ascii=False, separators=(",", ":"))
        ),
        "template_body_sha256": binding["body_sha256"],
        "required_media": binding["media"],
        "provider_components_sha256": binding["provider_components_sha256"],
        "provider_api_metadata_verified": binding["provider_api_metadata_verified"],
        "source_identity": projection["identity"],
        "domain_authorized": False,
        "dispatch_allowed": False,
        "blockers": projection["blockers"]
        + ["provider_template_components_unverified"]
        + (["independent_media_header_proof_required"] if binding["media"] else []),
    }


@sensitive_variables()
def verify_prepared(
    planned, *, scenario, kind, event=None, stay=None, delivery=None, existing_url=None
):
    if kind == "payment_success":
        projection = payment_success_projection(event, scenario)
    elif kind == "payment_link":
        projection = payment_link_projection(stay, delivery, scenario, existing_url=existing_url)
    else:
        return hold("source_kind_not_financial")
    policy = independent_template_policy(projection)
    if policy.get("available") is not True:
        return policy
    if not isinstance(planned, dict) or (
        planned.get("language") != policy["language"]
        or planned.get("message_input") != policy["message"]
        or planned.get("content_digest") != policy["content_digest"]
    ):
        return hold("prepared_financial_template_mismatch")
    return {**policy, "content_matches": True, "source_ack_allowed": False}
