"""Candidate-only source compatibility: synthetic data, no producer or provider."""

import json
import os
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from unittest.mock import Mock
from uuid import uuid4

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext

from apps.payments.payment_links import fingerprint as legacy_fingerprint
from apps.reservations.extension_contact_compat import (
    ADDRESS_LIMITS,
    CONTRACT,
    ContactMismatch,
    extension_modification_values,
    financial_fingerprint_v2,
    project_current_contact,
    recipient_message_key,
)
from apps.reservations.stay_extensions import chain_context, refresh_chain
from tests import test_stay_extensions as original
from tests.test_reservation_payment_links import manual_stay

setup_extension = original.setup_extension
create_child = original.create_child


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    monkeypatch.setattr("httpx.Client.request", Mock(side_effect=AssertionError("no live IO")))


def packet(*, chain_id=None, booking="91001", revision=1, generation=0, phone="+966500000000"):
    return {
        "contract": CONTRACT,
        "chain_id": str(chain_id or uuid4()),
        "current": {
            "row_id": str(uuid4()),
            "previous_row_id": None,
            "reservation_key": booking,
            "scenario": "01",
            "revision": revision,
            "event_key": "synthetic:correction:" + uuid4().hex,
            "source_updated_at": datetime(2026, 11, 28, tzinfo=UTC).isoformat(),
            "listing_map_id": 8001,
            "version_status": "current",
            "change_type": "personal_update" if generation == 0 else "phone_update",
            "recipient_generation": generation,
            "contact": {
                "first_name": "Synthetic",
                "last_name": "Corrected",
                "email": "corrected@example.invalid",
                "phone": phone,
                "language": "ar",
                "address": {k: "" for k in ADDRESS_LIMITS},
            },
        },
    }


def project(p, previous=None):
    return project_current_contact(
        p,
        chain_id=p["chain_id"],
        listing_map_id=8001,
        scenario="01",
        member_keys={"91001", "92001"},
        baseline_phone="+966500000000",
        previous=previous,
    )


def successor(p):
    q = deepcopy(p)
    q["current"].update(
        row_id=str(uuid4()),
        previous_row_id=p["current"]["row_id"],
        revision=p["current"]["revision"] + 1,
        event_key="synthetic:correction:" + uuid4().hex,
        source_updated_at=(
            datetime.fromisoformat(p["current"]["source_updated_at"]) + timedelta(hours=1)
        ).isoformat(),
    )
    return q


def test_personal_version_changes_contact_without_new_recipient_or_message_identity():
    p = packet()
    first = project(p)
    q = successor(p)
    q["current"]["contact"].update(first_name="Other", email="other@example.invalid")
    q["current"]["contact"]["address"]["billing_city"] = "Riyadh"
    second = project(q, first)
    assert second.contact()["first_name"] == "Other"
    assert second.recipient_generation == first.recipient_generation == 0
    assert second.binding_sha256 != first.binding_sha256
    assert recipient_message_key(
        booking_key="91001", message_key="confirmation", recipient_generation=0
    ) == recipient_message_key(
        booking_key="91001",
        message_key="confirmation",
        recipient_generation=second.recipient_generation,
    )
    exposed = second.contact()
    exposed["phone"] = "changed externally"
    assert second.contact()["phone"] == "+966500000000"
    assert "other@example.invalid" not in repr(second)


def test_new_phone_has_one_new_identity_even_with_old_delivered_or_unknown_history():
    p = packet()
    old = project(p)
    old_ledgers = {"confirmation": "delivered", "guide": "unknown", "rules": "failed"}
    history = deepcopy(old_ledgers)
    q = successor(p)
    q["current"].update(change_type="phone_update", recipient_generation=1)
    q["current"]["contact"]["phone"] = "+966551234567"
    new = project(q, old)
    assert project(q, new) is new
    assert new.recipient_generation == 1 and old_ledgers == history
    for occurrence in old_ledgers:
        assert recipient_message_key(
            booking_key="91001", message_key=occurrence, recipient_generation=0
        ) != recipient_message_key(
            booking_key="91001", message_key=occurrence, recipient_generation=1
        )


def test_phone_format_change_is_not_another_recipient_and_old_source_age_is_not_a_veto():
    p = packet(phone="+966 50 000 0000")
    p["current"]["change_type"] = "phone_update"
    p["current"]["source_updated_at"] = "2025-01-01T00:00:00+00:00"
    assert project(p).recipient_generation == 0


def test_first_corrected_phone_with_no_previous_number_does_not_add_an_extra_veto():
    p = packet(generation=1)
    result = project_current_contact(
        p,
        chain_id=p["chain_id"],
        listing_map_id=8001,
        scenario="01",
        member_keys={"91001"},
        baseline_phone="",
    )
    assert result.recipient_generation == 1


def test_export_contact_wire_fixtures_from_actual_adapter(tmp_path):
    p = packet()
    old = project(p)
    personal = successor(p)
    personal["current"]["contact"]["email"] = "changed@example.invalid"
    edited = project(personal, old)
    phone = successor(personal)
    phone["current"].update(change_type="phone_update", recipient_generation=1)
    phone["current"]["contact"]["phone"] = "+966551234567"
    corrected = project(phone, edited)
    cancelled = successor(phone)
    cancelled["current"]["change_type"] = "cancelled"
    carry = project(cancelled, corrected)
    output = Path(os.environ.get("LSA_EXTENSION_CONTACT_FIXTURE_DIR", tmp_path))
    output.mkdir(parents=True, exist_ok=True)
    (output / "contact-wire.synthetic.json").write_text(
        json.dumps(
            {
                "synthetic_only": True,
                "packets": [p, personal, phone, cancelled],
                "expected_generations": [
                    old.recipient_generation,
                    edited.recipient_generation,
                    corrected.recipient_generation,
                    carry.recipient_generation,
                ],
                "expected_binding_sha256": [
                    old.binding_sha256,
                    edited.binding_sha256,
                    corrected.binding_sha256,
                    carry.binding_sha256,
                ],
                "historical_provider_outcomes": "never modified by this adapter",
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )


@pytest.mark.parametrize(
    "field,value",
    [
        ("reservation_key", "99999"),
        ("reservation_key", {"phone": "sensitive"}),
        ("listing_map_id", 8002),
        ("listing_map_id", True),
        ("version_status", "superseded"),
        ("scenario", []),
        ("scenario", "06"),
        ("change_type", "new_booking"),
        ("revision", True),
        ("source_updated_at", "2026-11-28T00:00:00"),
        ("recipient_generation", 2),
    ],
)
def test_untrusted_shapes_or_cross_scope_never_become_contact_authority(field, value):
    p = packet()
    p["current"][field] = value
    with pytest.raises(ContactMismatch) as caught:
        project(p)
    assert "sensitive" not in str(caught.value)


def test_conflicting_duplicate_and_stale_or_wrong_predecessor_cannot_replace_current_contact():
    p = packet()
    current = project(p)
    q = deepcopy(p)
    q["current"]["contact"]["email"] = "conflict@example.invalid"
    with pytest.raises(ContactMismatch, match="immutable_source_row_conflict"):
        project(q, current)
    q = successor(p)
    q["current"]["previous_row_id"] = str(uuid4())
    with pytest.raises(ContactMismatch, match="contact_version_chain_mismatch"):
        project(q, current)
    q["current"]["source_updated_at"] = p["current"]["source_updated_at"]
    with pytest.raises(ContactMismatch, match="stale_or_ambiguous_contact"):
        project(q, current)


def test_extra_authorization_field_is_not_a_source_grant():
    p = packet()
    p["current"]["authorized"] = True
    with pytest.raises(ContactMismatch, match="current_row_shape"):
        project(p)


def test_scenario06_is_accepted_only_with_its_actual_source_owner_scope():
    p = packet()
    p["current"]["scenario"] = "06"
    result = project_current_contact(
        p,
        chain_id=p["chain_id"],
        listing_map_id=8001,
        scenario="06",
        member_keys={"91001"},
        baseline_phone="+966500000000",
    )
    assert result.recipient_generation == 0


def test_another_members_personal_update_cannot_restore_a_previous_phone():
    p = packet(booking="92001", generation=1, phone="+966551234567")
    current = project(p)
    q = successor(p)
    q["current"].update(
        reservation_key="91001", change_type="personal_update", recipient_generation=0
    )
    q["current"]["contact"]["phone"] = "+966500000000"
    with pytest.raises(ContactMismatch, match="contact_requires_current_correction"):
        project(q, current)


@pytest.mark.parametrize("field", ["chain_id", "binding_sha256", "recipient_generation"])
def test_projection_identity_cannot_be_mutated_after_validation(field):
    from dataclasses import FrozenInstanceError

    value = project(packet())
    with pytest.raises(FrozenInstanceError):
        setattr(value, field, "changed")


@pytest.mark.django_db
def test_cancelled_child_does_not_reselect_an_older_draft_contact(setup_extension, monkeypatch):
    case = setup_extension
    create_child(case, monkeypatch)
    p = packet(
        chain_id=case.extension.chain_id, booking="92001", generation=1, phone="+966551234567"
    )
    current = project(p)
    reservation = case.extension.reservation
    reservation.normalized_status = "cancelled"
    reservation.save()
    refresh_chain(case.extension.chain_id)
    case.extension.chain.refresh_from_db()
    assert chain_context(case.extension.chain)["terminal_hostaway_reservation_id"] == 91001
    cancelled = successor(p)
    cancelled["current"]["change_type"] = "cancelled"
    with CaptureQueriesContext(connection) as queries:
        retained = project(cancelled, current)
    assert not queries and retained.recipient_generation == current.recipient_generation
    assert retained.contact()["phone"] == "+966551234567"
    cancelled["current"]["contact"]["phone"] = "+966500000000"
    with pytest.raises(ContactMismatch, match="cancelled_contact_must_carry_current"):
        project(cancelled, current)


@pytest.mark.django_db
def test_financial_v2_survives_pii_correction_while_legacy_authority_is_not_rewritten():
    reservation = manual_stay()
    before = financial_fingerprint_v2(reservation)
    legacy = legacy_fingerprint(reservation)
    intent = reservation.booking_intent
    intent.guest_first_name, intent.guest_last_name = "Corrected", "Guest"
    intent.guest_phone, intent.guest_email = "+966551234567", "other@example.invalid"
    intent.billing_city = "Riyadh"
    intent.language = "fr"
    assert financial_fingerprint_v2(reservation) == before
    assert legacy_fingerprint(reservation) != legacy
    assert reservation.payment_requests.count() == 0
    reservation.total_price += Decimal("1.00")
    assert financial_fingerprint_v2(reservation) != before


@pytest.mark.django_db
@pytest.mark.parametrize(
    "field",
    [
        "check_out",
        "hostaway_reservation_id",
        "hostaway_listing_map_id",
        "guests",
        "is_test",
        "currency",
        "amount",
        "fx",
    ],
)
def test_financial_v2_still_detects_each_current_financial_or_booking_identity_change(field):
    reservation = manual_stay()
    before = financial_fingerprint_v2(reservation)
    if field == "amount":
        reservation.booking_intent.payment_amount_sar += Decimal("0.01")
    elif field == "fx":
        reservation.booking_intent.exchange_rate_snapshot = {"changed": True}
    elif field == "check_out":
        reservation.check_out += timedelta(days=1)
    elif field == "is_test":
        reservation.is_test = not reservation.is_test
    elif field == "currency":
        reservation.currency = "MAD"
    else:
        setattr(reservation, field, getattr(reservation, field) + 1)
    assert financial_fingerprint_v2(reservation) != before


@pytest.mark.django_db
@pytest.mark.parametrize("language", ["ar", "en", "fr"])
def test_verified_unpaid_child_uses_existing_update_template_without_paid_claim(
    setup_extension, monkeypatch, language
):
    case = setup_extension
    create_child(case, monkeypatch)
    case.property.name_ar = "شقة تجريبية"
    case.property.name_en = "Synthetic apartment"
    case.property.name_fr = "Appartement fictif"
    case.property.save()
    case.extension.chain.property = case.property
    assert case.extension.state == "created_unpaid"
    p = packet(chain_id=case.extension.chain_id, booking="92001")
    p["current"]["contact"]["language"] = language
    with CaptureQueriesContext(connection) as queries:
        notice = extension_modification_values(case.extension, project(p))
    assert all(q["sql"].lstrip().upper().startswith("SELECT") for q in queries)
    assert notice["reservation_key"] == "92001"
    assert notice["variables"]["arrival_date"] == case.raw["departureDate"]
    assert notice["variables"]["departure_date"] == case.extension.draft.check_out.isoformat()
    assert notice["template_name"] == "lsa_booking_updated_v2"
    assert (
        notice["purpose"] == "booking_change" and notice["source_message_type"] == "status_modified"
    )
    assert notice["payment_authorized"] is notice["send_authorized"] is False
    bindings = json.loads(
        (
            Path(__file__).parents[1]
            / "apps/reservations/template_v2_assets/template-source-bindings-v2.json"
        ).read_text()
    )["templates"]
    actual = next(
        r for r in bindings if r["name"] == notice["template_name"] and r["language"] == language
    )
    assert actual["ordered_source_keys"] == notice["ordered_source_keys"]
    assert actual["asset"] is None
    assert list(notice["variables"]) == actual["ordered_source_keys"]
    if output_dir := os.environ.get("LSA_EXTENSION_CONTACT_FIXTURE_DIR"):
        output = Path(output_dir)
        output.mkdir(parents=True, exist_ok=True)
        (output / f"notice-values-{language}.synthetic.json").write_text(
            json.dumps(
                {
                    "synthetic_only": True,
                    "notice": notice,
                    "existing_template_body_sha256": actual["body_sha256"],
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
