from copy import deepcopy

import pytest

from apps.reservations import message_source_authorization as source
from apps.reservations import website_access_source_v2 as access
from apps.reservations.website_financial_source_v2 import variables
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
    local = variables(stay, scenario)
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
