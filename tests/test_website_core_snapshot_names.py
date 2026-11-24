"""Use real current source events and source20's documented snapshot name projection."""

from copy import deepcopy

import pytest
from django.db import connection, transaction
from django.test.utils import CaptureQueriesContext

from apps.reservations import message_source_authorization as source
from apps.reservations import website_financial_source_v2 as financial
from apps.reservations import website_template_policy_v2 as policy
from apps.reservations import website_template_selector_v2 as selector
from tests import test_message_source_authorization as original

record = original.record
pytestmark = pytest.mark.django_db


def packet(record, language, kind):
    scenario, stay, _ = record
    type(stay.property).objects.filter(pk=stay.property_id).update(
        name_ar="شقة المصدر الموثق", name_en="Source apartment", name_fr="Appartement source"
    )
    type(stay).objects.filter(pk=stay.pk).update(guest_language=language)
    with transaction.atomic():
        source.SOURCES[scenario][4].capture_event(stay.pk)
    stay.refresh_from_db()
    event = stay.latest_communication_event()
    facts = source.core_facts(
        stay, event, scenario, source.sha(source.core_keys(stay.pk, event.payload, scenario)[kind])
    )
    context = {key: facts[key] for key in selector.IDENTITY}
    # automation_template_values(p,a,lang) reads listing_name from p, exactly;
    # it does not choose a translated Django property name for the core path.
    values = {
        **financial.variables(stay, scenario),
        "listing_name": event.payload["listing_name"],
        "property_address": "SYNTHETIC ADDRESS",
        "map_url": "https://example.invalid/map",
        "video_url": "https://example.invalid/video",
        "gate_code": "NOT_A_REAL_CODE",
        "wifi_net": "SYNTHETIC_NETWORK",
        "wifi_pass": "NOT_A_REAL_PASSWORD",
        "booking_platform": "Luxury Smart Apartments",
    }
    context.update(
        source_family=f"lsa{scenario}",
        recipient_ref=None,
        recipient_binding_sha256=source.sha(
            facts["reservation_key"] + ":" + facts["recipient_e164"]
        ),
        language=language,
        not_before=facts["not_before"],
        expires_at=facts["expires_at"],
        source_context_ref="synthetic-source20-core-snapshot",
        values=values,
    )
    return scenario, stay, event, facts, context


@pytest.mark.parametrize("language", ["ar", "en", "fr"])
@pytest.mark.parametrize("kind", ["status_modified", "house_rules", "checkin_guide"])
def test_source20_core_snapshot_name_matches_in_all_languages_without_a_write(
    record, language, kind
):
    scenario, stay, event, facts, context = packet(record, language, kind)
    with CaptureQueriesContext(connection) as queries:
        result = policy.policy_from_source(event, facts, context)
    assert result is not None
    assert result["message"]["mode"] == "template"
    assert context["values"]["listing_name"] == "شقة المصدر الموثق"
    assert financial.variables(stay, scenario)["listing_name"] == getattr(
        stay.property, "name_" + language
    )
    assert not any(
        q["sql"].lstrip().split()[0].upper() in {"INSERT", "UPDATE", "DELETE"} for q in queries
    )
    if kind == "checkin_guide":
        assert result["message"]["attachment"]["kind"] == "document"
    other = deepcopy(context)
    other["values"]["listing_name"] = "FOREIGN PROPERTY NAME"
    assert policy.policy_from_source(event, facts, other) is None


def test_valid_core_snapshot_does_not_require_a_new_translated_financial_label(record):
    scenario, stay, event, facts, context = packet(record, "fr", "checkin_guide")
    type(stay.property).objects.filter(pk=stay.property_id).update(name_en="", name_fr="")
    stay.property.refresh_from_db()
    assert (
        financial.core_variables(event, scenario)["listing_name"]
        == context["values"]["listing_name"]
    )
    assert policy.policy_from_source(event, facts, context) is not None
    with pytest.raises(ValueError):
        financial.variables(stay, scenario)


def test_matching_context_cannot_launder_a_changed_core_event_name(record):
    scenario, _, event, facts, context = packet(record, "en", "status_modified")
    changed = {**event.payload, "listing_name": "FOREIGN PROPERTY NAME"}
    type(event).objects.filter(pk=event.pk).update(payload=changed)
    context["values"]["listing_name"] = changed["listing_name"]
    assert policy.policy_from_source(event, facts, context) is None
