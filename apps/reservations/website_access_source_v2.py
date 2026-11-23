"""Reconcile private source20 property context with current website stay values.

The caller must obtain context from the fixed authenticated render-source view.
This module has no HTTP route, downloads, PDF writer, ACK or provider operation.
"""

import re
from urllib.parse import urlsplit

from django.utils import timezone
from django.utils.dateparse import parse_datetime
from django.views.decorators.debug import sensitive_variables

from . import message_source_authorization as source
from .website_financial_source_v2 import hold, variables

DOCUMENT_FIELDS = (
    "guest_name",
    "booking_reference",
    "listing_name",
    "arrival_date",
    "departure_date",
    "check_in_time",
    "check_out_time",
    "timezone",
    "property_address",
    "map_url",
    "video_url",
    "gate_code",
    "wifi_net",
    "wifi_pass",
)
LOCAL_FIELDS = DOCUMENT_FIELDS[:8]
IDENTITY_FIELDS = (
    "scenario",
    "job_sha256",
    "reservation_key",
    "reservation_revision",
    "recipient_ref",
    "source_event_key",
    "source_message_type",
    "purpose",
    "language",
)


@sensitive_variables()
def access_source_values(event, facts, context):
    """Fail closed if any of the14 fields or its booking/recipient context differ."""
    scenario = facts.get("scenario")
    if (
        scenario not in source.SOURCES
        or not isinstance(event, source.SOURCES[scenario][1])
        or facts.get("source_message_type") != "checkin_guide"
        or facts.get("purpose") != "checkin_guide"
        or not isinstance(context, dict)
    ):
        return hold("access_source_not_in_scope")
    if context.get("recipient_ref") is None:
        context = {**context, "recipient_ref": facts.get("recipient_ref")}
    event = type(event).objects.select_related("stay__property").get(pk=event.pk)
    stay = event.stay
    try:
        expected_key = source.core_keys(stay.pk, event.payload, scenario)["checkin_guide"]
        phone = source.normalize_phone_number(stay.guest_phone)
        expected = {
            "scenario": scenario,
            "job_sha256": source.sha(expected_key),
            "reservation_key": f"{'morning07' if scenario == '07' else 'dayuse08'}:{stay.pk}",
            "reservation_revision": str(stay.communication_revision),
            "source_event_key": f"website:{scenario}:core:{event.pk}",
            "source_message_type": "checkin_guide",
            "purpose": "checkin_guide",
            "language": stay.guest_language,
            "recipient_ref": "site01:"
            + source.salted_hmac(
                "website-source-recipient.v1", f"{scenario}:{stay.pk}:{phone}", algorithm="sha256"
            ).hexdigest(),
            "not_before": event.created_at.isoformat(),
            "expires_at": stay.ends_at.isoformat(),
        }
        if (
            not source.current_core(stay, event, source.SOURCES[scenario][4])
            or not source.SOURCES[scenario][4].admitted(stay)
            or stay.status == "cancelled"
            or stay.ends_at <= timezone.now()
            or any(facts.get(k) != v for k, v in expected.items())
            or not all(
                type(context.get(k)) is type(facts.get(k))
                and context.get(k) == facts.get(k)
                and isinstance(facts.get(k), str)
                and facts[k]
                for k in IDENTITY_FIELDS
            )
            or context.get("source_family") != f"lsa{scenario}"
            or not isinstance(context.get("source_context_ref"), str)
            or not context["source_context_ref"]
            or facts["reservation_revision"] != str(stay.communication_revision)
            or facts["recipient_e164"] != source.normalize_phone_number(stay.guest_phone)
            or context.get("recipient_binding_sha256")
            != source.sha(f"{facts['reservation_key']}:{facts['recipient_e164']}")
        ):
            return hold("access_source_identity_unverified")
        for key in ("not_before", "expires_at"):
            actual, expected = parse_datetime(context[key]), parse_datetime(facts[key])
            if actual is None or not timezone.is_aware(actual) or actual != expected:
                return hold("access_source_window_unverified")
        local = variables(stay, scenario)
        values = context.get("values")
        if not isinstance(values, dict) or any(
            not isinstance(values.get(k), str) or not values[k].strip() or len(values[k]) > 4096
            for k in DOCUMENT_FIELDS
            if k != "gate_code"
        ):
            return hold("access_document_field_missing")
        if "gate_code" not in values or not isinstance(values["gate_code"], str):
            return hold("access_document_field_missing")
        instruction_used = not values["gate_code"].strip()
        if instruction_used:
            instruction = values.get("gate_access_instruction")
            evidence = context.get("gate_access_evidence_ref")
            if (
                not isinstance(instruction, str)
                or not instruction.strip()
                or len(instruction) > 2048
                or not isinstance(evidence, str)
                or not re.fullmatch(r"[!-~]{1,256}", evidence)
            ):
                return hold("access_gate_instruction_unverified")
        elif len(values["gate_code"]) > 4096:
            return hold("access_document_field_missing")
        if any(values[k] != local[k] for k in LOCAL_FIELDS):
            return hold("access_document_source_mismatch")
        for key in ("map_url", "video_url"):
            url = urlsplit(values[key])
            if url.scheme != "https" or not url.hostname or url.username or url.password:
                return hold("access_document_url_unverified")
        return {
            "available": True,
            "contract_version": "site01-access-values-2",
            "values": {k: values[k] for k in DOCUMENT_FIELDS}
            | ({"gate_access_instruction": instruction} if instruction_used else {}),
            "source_context_ref": context["source_context_ref"],
            "dispatch_allowed": False,
        }
    except (KeyError, ValueError, TypeError):
        return hold("access_source_unverified")
