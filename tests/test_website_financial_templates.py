"""Synthetic database records; no network, provider calls or source ACK writes."""

from copy import deepcopy
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext

from apps.reservations import message_source_authorization as source
from apps.reservations import website_financial_templates as templates
from tests import test_message_source_authorization as source_tests

pytestmark = pytest.mark.django_db
record = source_tests.record
paid_record = source_tests.paid_record


@pytest.fixture(autouse=True)
def translated_source_labels(record):
    stay = record[1]
    type(stay.property).objects.filter(pk=stay.property_id).update(
        name_ar="شقة اختبار مصطنعة",
        name_en="Synthetic Test Apartment",
        name_fr="Appartement de test",
    )
    stay.property.refresh_from_db()


def link_record(record):
    from apps.reservations import day_use_payment_messages, morning_payment_messages

    scenario, stay, _ = record
    links = morning_payment_messages if scenario == "07" else day_use_payment_messages
    row = stay.payment_link_deliveries.create(fingerprint=links.fingerprint(stay))
    # Synthetic test only: source candidate must never call this URL minter.
    url = links.public_url(stay)
    return stay, row, url, links


@pytest.mark.parametrize("language", ["ar", "en", "fr"])
@pytest.mark.parametrize("kind", ["payment_success", "payment_link"])
def test_exact_twelve_bindings_from_current_source_and_no_writes(
    record, language, kind, monkeypatch
):
    scenario, stay, event = record
    type(stay).objects.filter(pk=stay.pk).update(guest_language=language)
    stay.refresh_from_db()
    if kind == "payment_success":
        _, stay, event = paid_record((scenario, stay, event))
        with CaptureQueriesContext(connection) as queries:
            projection = templates.payment_success_projection(event, scenario)
        assert projection["identity"]["financial_proof_ref"] == event.proof_key
        assert projection["identity"]["payment_revision"] == event.payment_revision
        expected_keys = templates.SUCCESS_KEYS
    else:
        stay, row, url, links = link_record((scenario, stay, event))
        monkeypatch.setattr(links, "public_url", lambda *_: pytest.fail("must not mint link"))
        with CaptureQueriesContext(connection) as queries:
            projection = templates.payment_link_projection(stay, row, scenario, existing_url=url)
        assert projection["variables"]["payment_url"] == url
        assert projection["identity"]["url_has_clock_expiry"] is False
        assert projection["identity"]["send_window_ends_at"] == stay.ends_at.isoformat()
        expected_keys = templates.LINK_KEYS
    assert queries.captured_queries
    assert all(q["sql"].lstrip().upper().startswith("SELECT") for q in queries.captured_queries)
    policy = templates.independent_template_policy(projection)
    assert policy["available"] is True
    expected_message = {
        "mode": "template",
        "template_ref": f"lsa{scenario}_{kind}_v1",
        "variables": [projection["variables"][k] for k in expected_keys],
    }
    if scenario == "07" and kind == "payment_link":
        expected_message["header"] = {
            "type": "image",
            "source_sha256": policy["required_media"]["sha256"],
        }
    assert policy["message"] == expected_message
    assert projection["variables"]["amount_sar"] == "500.00"
    prefix = "AM-" if scenario == "07" else "DU-"
    assert projection["variables"]["booking_reference"] == prefix + str(stay.pk)[:8]
    assert projection["variables"]["starts_at_local"] == stay.starts_at.astimezone(
        ZoneInfo(stay.time_zone)
    ).strftime("%Y-%m-%d %H:%M")
    assert projection["currency"] == "SAR"
    assert projection["reservation_revision"] == str(stay.communication_revision)
    assert policy["dispatch_allowed"] is False and policy["domain_authorized"] is False
    assert policy["provider_components_sha256"] is None
    assert policy["provider_api_metadata_verified"] is False
    if scenario == "07" and kind == "payment_link":
        asset = (
            templates.files("apps.reservations")
            .joinpath("morning_assets", f"payment-{language}.png")
            .read_bytes()
        )
        assert policy["required_media"]["sha256"] == templates.hashlib.sha256(asset).hexdigest()
        assert "independent_media_header_proof_required" in policy["blockers"]
    else:
        assert policy["required_media"] is None


def test_link_is_held_without_persisted_resource_and_does_not_mint(record, monkeypatch):
    stay, row, _, links = link_record(record)
    monkeypatch.setattr(links, "public_url", lambda *_: pytest.fail("must not mint link"))
    assert templates.payment_link_projection(stay, row, record[0]) == templates.hold(
        "existing_payment_url_not_bound"
    )


def test_link_image_cannot_be_dropped_or_changed_in_planned_content(record):
    stay, row, url, _ = link_record(record)
    projection = templates.payment_link_projection(stay, row, record[0], existing_url=url)
    policy = templates.independent_template_policy(projection)
    planned = {
        "language": policy["language"],
        "message_input": deepcopy(policy["message"]),
        "content_digest": policy["content_digest"],
    }
    result = templates.verify_prepared(
        planned, scenario=record[0], kind="payment_link", stay=stay, delivery=row, existing_url=url
    )
    assert result["content_matches"] is True and result["source_ack_allowed"] is False
    if record[0] == "07":
        del planned["message_input"]["header"]
    else:
        planned["message_input"]["header"] = {"type": "image", "source_sha256": "a" * 64}
    assert (
        templates.verify_prepared(
            planned,
            scenario=record[0],
            kind="payment_link",
            stay=stay,
            delivery=row,
            existing_url=url,
        )["available"]
        is False
    )


@pytest.mark.parametrize("change", ["host", "scheme", "query", "fragment", "token", "route"])
def test_existing_url_must_be_exact_signed_current_source_resource(record, change):
    stay, row, url, _ = link_record(record)
    changed = {
        "host": url.replace("example.invalid", "attacker.invalid"),
        "scheme": url.replace("https:", "http:"),
        "query": url + "?redirect=attacker",
        "fragment": url + "#extra",
        "token": url[:-2] + "x/",
        "route": "https://example.invalid/not-a-payment/",
    }[change]
    assert (
        templates.payment_link_projection(stay, row, record[0], existing_url=changed)["available"]
        is False
    )


@pytest.mark.parametrize("state", ["posting", "uncertain", "accepted", "sent", "failed"])
def test_existing_attempt_cannot_be_adopted_as_fresh_link(record, state):
    stay, row, url, _ = link_record(record)
    type(row).objects.filter(pk=row.pk).update(state=state)
    assert (
        templates.payment_link_projection(stay, row, record[0], existing_url=url)["available"]
        is False
    )


@pytest.mark.parametrize(
    "field,value",
    [
        ("status", "cancelled"),
        ("amount_sar", Decimal("501.00")),
        ("guest_phone", "+442079460001"),
        ("communication_revision", 999),
    ],
)
def test_payment_confirmation_rechecks_real_collection_and_snapshot(record, field, value):
    scenario, stay, event = paid_record(record)
    type(stay).objects.filter(pk=stay.pk).update(**{field: value})
    assert templates.payment_success_projection(event, scenario)["available"] is False


@pytest.mark.parametrize("mutation", ["order", "value", "name", "language", "digest"])
def test_planned_template_cannot_replace_source_values_or_become_ack(record, mutation):
    scenario, _, event = paid_record(record)
    projection = templates.payment_success_projection(event, scenario)
    policy = templates.independent_template_policy(projection)
    planned = {
        "language": policy["language"],
        "message_input": policy["message"],
        "content_digest": policy["content_digest"],
    }
    valid = templates.verify_prepared(
        planned, scenario=scenario, kind="payment_success", event=event
    )
    assert valid["content_matches"] is True and valid["source_ack_allowed"] is False
    changed = deepcopy(planned)
    if mutation == "order":
        changed["message_input"]["variables"].reverse()
    elif mutation == "value":
        changed["message_input"]["variables"][-2] = "9999.99"
    elif mutation == "name":
        changed["message_input"]["template_ref"] = "lsa07_payment_link_v1"
    elif mutation == "language":
        changed["language"] = "fr"
    else:
        changed["content_digest"] = "0" * 64
    assert (
        templates.verify_prepared(changed, scenario=scenario, kind="payment_success", event=event)[
            "available"
        ]
        is False
    )


def test_read_candidate_does_not_open_current_financial_authority_or_core_renderer(record):
    from apps.reservations.message_receipts import independent_render_policy

    scenario, stay, event = paid_record(record)
    key = source.SOURCES[scenario][5].reference(event.pk, "payment_success")[0]
    facts = source.payment_facts(
        stay, event, scenario, source.sha(key), source.SOURCES[scenario][5]
    )
    assert facts["domain_authorized"] is False and facts["dispatch_allowed"] is False
    assert independent_render_policy(event, facts, None) is None


def test_no_owner_or_purpose_alias(record):
    scenario, _, event = paid_record(record)
    other = "08" if scenario == "07" else "07"
    assert templates.payment_success_projection(event, other)["available"] is False
    assert (
        templates.verify_prepared({}, scenario=scenario, kind="loyalty_invitation")["available"]
        is False
    )


def test_missing_source_label_holds_instead_of_inventing_name(record):
    scenario, stay, event = paid_record(record)
    type(stay.property).objects.filter(pk=stay.property_id).update(
        name_ar="", name_en="", name_fr=""
    )
    assert templates.payment_success_projection(event, scenario) == templates.hold(
        "source_variables_unverified"
    )
