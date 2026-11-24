from copy import deepcopy

import pytest

from apps.reservations import message_source_authorization as source
from apps.reservations import website_access_source_v2 as access
from apps.reservations.website_financial_source_v2 import core_variables
from tests import test_message_source_authorization as originals

record = originals.record
pytestmark = pytest.mark.django_db


@pytest.fixture
def packet(record):
    scenario, stay, event = record
    type(stay.property).objects.filter(pk=stay.property_id).update(name_en="Synthetic Apartment")
    stay.property.refresh_from_db()
    source.SOURCES[scenario][4].capture_event(stay.pk)
    stay.refresh_from_db()
    event = stay.latest_communication_event()
    locator = source.sha(source.core_keys(stay.pk, event.payload, scenario)["checkin_guide"])
    facts = source.core_facts(stay, event, scenario, locator)
    local = core_variables(event, scenario)
    context = {key: facts[key] for key in access.IDENTITY_FIELDS}
    context.update(
        source_family=f"lsa{scenario}",
        source_context_ref="yc20-template-source-2:synthetic-only",
        not_before=facts["not_before"],
        expires_at=facts["expires_at"],
        recipient_binding_sha256=source.sha(
            f"{facts['reservation_key']}:{facts['recipient_e164']}"
        ),
        values={
            **{key: local[key] for key in access.LOCAL_FIELDS},
            "property_address": "SYNTHETIC ADDRESS",
            "map_url": "https://example.invalid/map",
            "video_url": "https://example.invalid/video",
            "gate_code": "NOT_A_REAL_CODE",
            "wifi_net": "SYNTHETIC_NETWORK",
            "wifi_pass": "NOT_A_REAL_PASSWORD",
        },
    )
    return event, facts, context


def test_exact_fourteen_fields_from_independent_source_no_ack(packet):
    result = access.access_source_values(*packet)
    assert result["available"], result
    assert tuple(result["values"]) == access.DOCUMENT_FIELDS
    assert result["dispatch_allowed"] is False
    assert "pdf" not in result and "message" not in result


@pytest.mark.parametrize("field", access.DOCUMENT_FIELDS)
def test_no_access_field_is_silently_dropped_or_filled_from_sample(packet, field):
    event, facts, context = packet
    del context["values"][field]
    assert access.access_source_values(event, facts, context)["available"] is False


@pytest.mark.parametrize("field", access.LOCAL_FIELDS)
def test_source_context_cannot_replace_guest_dates_reference_or_zone(packet, field):
    event, facts, context = packet
    context["values"][field] = "CHANGED_SYNTHETIC_VALUE"
    assert access.access_source_values(event, facts, context)["available"] is False


@pytest.mark.parametrize("field", access.IDENTITY_FIELDS)
def test_context_cannot_cross_recipient_revision_source_job_or_purpose(packet, field):
    event, facts, context = packet
    context[field] = "changed"
    assert access.access_source_values(event, facts, context)["available"] is False


def test_matching_context_and_facts_still_cannot_replace_actual_domain(packet):
    event, facts, context = packet
    facts = deepcopy(facts)
    for key in ("reservation_key", "job_sha256", "source_event_key", "recipient_ref"):
        facts[key] = context[key] = "borrowed-from-other-source"
    assert access.access_source_values(event, facts, context)["available"] is False


@pytest.mark.parametrize(
    "value",
    [
        "http://example.invalid/map",
        "https://user:password@example.invalid/map",
        "file:///tmp/sample.pdf",
    ],
)
def test_source_url_is_a_service_link_not_an_arbitrary_download(packet, value):
    event, facts, context = packet
    context["values"]["map_url"] = value
    assert access.access_source_values(event, facts, context)["available"] is False


@pytest.mark.parametrize(
    "instruction", ["No external gate (synthetic)", "Security at entrance (synthetic)"]
)
def test_explicit_private_owner_instruction_is_distinct_from_a_missing_code(packet, instruction):
    event, facts, context = packet
    context["values"].update(gate_code="", gate_access_instruction=instruction)
    context["gate_access_evidence_ref"] = "synthetic-owner-gate:20261124"
    result = access.access_source_values(event, facts, context)
    assert result["available"] is True
    assert result["values"]["gate_code"] == ""
    assert result["values"]["gate_access_instruction"] == instruction


@pytest.mark.parametrize(
    "missing", ["instruction", "evidence", "wrong_type", "control", "too_long"]
)
def test_blank_gate_never_becomes_permission_without_source_evidence(packet, missing):
    event, facts, context = packet
    context["values"].update(gate_code="", gate_access_instruction="SYNTHETIC ENTRANCE")
    context["gate_access_evidence_ref"] = "synthetic-owner-gate:20261124"
    if missing == "instruction":
        del context["values"]["gate_access_instruction"]
    elif missing == "evidence":
        del context["gate_access_evidence_ref"]
    elif missing == "wrong_type":
        context["values"]["gate_code"] = False
    elif missing == "control":
        context["gate_access_evidence_ref"] += "\n"
    else:
        context["values"]["gate_access_instruction"] = "x" * 2049
    assert access.access_source_values(event, facts, context)["available"] is False


def test_real_code_remains_authoritative_when_instruction_is_also_present(packet):
    event, facts, context = packet
    context["values"]["gate_access_instruction"] = "SYNTHETIC UNSELECTED INSTRUCTION"
    result = access.access_source_values(event, facts, context)
    assert result["available"] is True
    assert result["values"]["gate_code"] == "NOT_A_REAL_CODE"
    assert "gate_access_instruction" not in result["values"]


def test_independent_document_policy_rebuilds_current_source_and_binds_instruction_evidence(packet):
    from apps.reservations import website_template_policy_v2 as policy
    from apps.reservations import website_template_selector_v2 as selector
    from apps.reservations.website_template_contract_v2 import content_digest

    event, facts, context = packet
    context["values"].update(gate_code="", gate_access_instruction="SYNTHETIC BUILDING ENTRANCE")
    context["gate_access_evidence_ref"] = "synthetic-owner-gate:20261124"
    original = deepcopy(context)
    planned = policy.policy_from_source(event, facts, context)
    assert planned is not None
    assert planned["message"]["attachment"]["kind"] == "document"
    canonical = {
        **facts,
        "source_family": "lsa" + facts["scenario"],
        "source_owner_family": "website",
    }
    raw = selector.select_source_message(canonical, original).attachment_bytes
    context["gate_access_evidence_ref"] += ":new-evidence"
    rebound = policy.policy_from_source(event, facts, context)
    assert rebound is not None
    assert selector.select_source_message(canonical, context).attachment_bytes == raw
    assert content_digest(planned["language"], planned["message"]) != content_digest(
        rebound["language"], rebound["message"]
    )
    context["values"]["guest_name"] = "UNTRUSTED GUEST"
    assert policy.policy_from_source(event, facts, context) is None
