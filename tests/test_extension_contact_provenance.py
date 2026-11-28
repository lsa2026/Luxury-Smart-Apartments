"""Actual draft/intent/provider flow with synthetic in-memory provider only."""

import copy
import json
from unittest.mock import Mock

import pytest
from django.db import connection

from apps.reservations.extension_contact_source import (
    ExtensionRootRead,
    make_draft_provenance,
    validate_draft_provenance,
)
from apps.reservations.manual_bookings import create_manual_booking_in_hostaway
from apps.reservations.models import BookingIntent, HostawayReservationOperation
from tests.test_stay_extensions import FakeHostaway, create_child, finalize

pytest_plugins = ["tests.test_stay_extensions"]
pytestmark = pytest.mark.django_db


def service(case, provider):
    from apps.reservations.services.hostaway_booking import HostawayBookingService

    return HostawayBookingService(client=provider, availability_service=case.availability)


def ownership(monkeypatch):
    from apps.reservations.models import StayExtension

    def ack(extension_id, **kwargs):
        StayExtension.objects.filter(pk=extension_id).update(ownership_acknowledged=True)
        return True

    monkeypatch.setattr(
        "apps.reservations.stay_extension_communications.acknowledge_ownership", ack
    )


def test_persistent_provenance_refreshes_real_payload_before_new_intent(
    setup_extension, monkeypatch
):
    case = setup_extension
    finalize(case)
    case.extension.draft.refresh_from_db()
    proof = case.extension.draft.extension_contact_provenance
    assert proof["contract"] == "site01-extension-contact-provenance-1"
    assert "guest_phone" not in proof["explicit_fields"]
    assert "guest_email" in proof["explicit_fields"]
    assert case.raw["phone"] not in json.dumps(proof)
    assert case.raw["guestEmail"] not in json.dumps(proof)
    case.raw.update(phone="+966551234567", guestFirstName="حديث", guestAddress="20 Root Road")
    provider = FakeHostaway(case)
    reads = Mock(wraps=provider.get_reservation_invoice_data)
    provider.get_reservation_invoice_data = reads
    ownership(monkeypatch)
    result = create_manual_booking_in_hostaway(
        draft_id=case.extension.draft_id, booking_service=service(case, provider)
    )
    assert result.code == "created"
    assert len(provider.posts) == 1
    payload = provider.posts[0]
    assert payload["phone"] == "+966551234567"
    assert payload["guestFirstName"] == "حديث"
    assert payload["guestLastName"] == "مصحح"
    assert payload["guestEmail"] == "corrected@example.invalid"
    assert payload["guestAddress"] == "10 Test Road\nRiyadh"  # Existing provider address layout.
    assert result.reservation.booking_intent.billing_street1 == "10 Test Road"
    assert reads.call_count == 1  # The ownership phase reuses the same fresh root GET.
    assert result.reservation.booking_intent.guest_phone == "+966551234567"
    assert result.reservation.payment_status != "paid"
    case.extension.draft.refresh_from_db()
    after = case.extension.draft.extension_contact_provenance
    assert after["revision"] == proof["revision"] + 1
    assert after["field_sources"]["guest_phone"] == "hostaway_root"
    assert after["field_sources"]["guest_email"] == "administrator"
    assert after["root_hostaway_reservation_id"] == case.raw["id"]


def test_explicit_phone_wins_over_new_provider_phone(setup_extension, monkeypatch):
    case = setup_extension
    case.guest_phone_override = "+966551111111"
    finalize(case)
    case.raw["phone"] = "+966552222222"
    result, provider, _ = create_child(case, monkeypatch)
    assert result.code == "created"
    assert provider.posts[0]["phone"] == "+966551111111"


def test_explicit_clear_is_not_restored_and_invalid_contact_blocks_before_intent(
    setup_extension, monkeypatch
):
    case = setup_extension
    finalize(case)
    draft = case.extension.draft
    draft.refresh_from_db()
    draft.guest_phone = ""
    prior = draft.extension_contact_provenance
    draft.extension_contact_provenance = make_draft_provenance(
        case.extension,
        explicit_fields=[*prior["explicit_fields"], "guest_phone"],
        source_id=case.raw["id"],
        revision=prior["revision"] + 1,
    )
    draft.save()
    provider = FakeHostaway(case)
    result = create_manual_booking_in_hostaway(
        draft_id=draft.pk, booking_service=service(case, provider)
    )
    assert result.code == "extension_contact_requires_review"
    assert not BookingIntent.objects.exists()
    assert not HostawayReservationOperation.objects.exists()
    assert not provider.posts
    draft.refresh_from_db()
    assert draft.guest_phone == ""


@pytest.mark.parametrize("corruption", ["empty", "root", "draft", "hash", "foreign_notes"])
def test_missing_or_mismatched_provenance_never_adopts_old_draft(setup_extension, corruption):
    case = setup_extension
    finalize(case)
    draft = case.extension.draft
    draft.refresh_from_db()
    proof = copy.deepcopy(draft.extension_contact_provenance)
    if corruption == "empty":
        proof = {}
    elif corruption == "root":
        proof["root_hostaway_reservation_id"] += 1
    elif corruption == "draft":
        proof["draft_id"] = "foreign"
    elif corruption == "hash":
        proof["baseline_hmac"]["guest_phone"] = "0" * 64
    else:
        proof["notes"] = "not part of the provenance contract"
    draft.extension_contact_provenance = proof
    draft.save()
    provider = FakeHostaway(case)
    provider.get_reservation_invoice_data = Mock(side_effect=AssertionError("no read before proof"))
    result = create_manual_booking_in_hostaway(
        draft_id=draft.pk, booking_service=service(case, provider)
    )
    assert result.code == "extension_contact_requires_review"
    assert not BookingIntent.objects.exists() and not provider.posts


def test_unknown_retry_preserves_intent_and_provenance(setup_extension, monkeypatch):
    case = setup_extension
    result, provider, existing_service = create_child(case, monkeypatch, unknown=True)
    assert result.code == "hostaway_create_uncertain"
    intent = BookingIntent.objects.get()
    before = (intent.guest_phone, intent.guest_email, intent.idempotency_key)
    case.extension.draft.refresh_from_db()
    proof = copy.deepcopy(case.extension.draft.extension_contact_provenance)
    case.raw["phone"] = "+966552222222"
    create_manual_booking_in_hostaway(
        draft_id=case.extension.draft_id, booking_service=existing_service
    )
    intent.refresh_from_db()
    assert (intent.guest_phone, intent.guest_email, intent.idempotency_key) == before
    case.extension.draft.refresh_from_db()
    assert case.extension.draft.extension_contact_provenance == proof
    assert len(provider.posts) == 1


def test_root_failure_does_not_freeze_intent_or_change_money(setup_extension):
    case = setup_extension
    finalize(case)
    case.extension.draft.refresh_from_db()
    draft = case.extension.draft
    amount = (draft.final_total_price, draft.payment_amount_sar, draft.exchange_rate_snapshot)
    provider = FakeHostaway(case)
    provider.get_reservation_invoice_data = Mock(side_effect=RuntimeError("synthetic unavailable"))
    result = create_manual_booking_in_hostaway(
        draft_id=draft.pk, booking_service=service(case, provider)
    )
    assert result.code == "extension_contact_requires_review"
    draft.refresh_from_db()
    assert (
        draft.final_total_price,
        draft.payment_amount_sar,
        draft.exchange_rate_snapshot,
    ) == amount
    assert not BookingIntent.objects.exists() and not provider.posts


def test_persistent_revision_rejects_stale_form_save(setup_extension):
    from apps.reservations.extension_contact_source import draft_contact_values, provenance_for_save

    extension = setup_extension.extension
    data = {**draft_contact_values(extension.draft), "extension_contact_revision": 0}
    with pytest.raises(ValueError, match="revision_changed"):
        provenance_for_save(extension, data)
    assert validate_draft_provenance(extension)["revision"] == 1


def test_saved_address_fallback_does_not_claim_provider_origin(setup_extension, monkeypatch):
    from apps.reservations.extension_contact_source import source_field_origins

    case = setup_extension
    monkeypatch.setattr(
        "apps.reservations.extension_contact_source.billing_initial",
        lambda raw: {"billing_street1": "Saved street"},
    )
    origins = source_field_origins(case.raw, case.raw["id"])
    assert origins["billing_street1"] == "local_saved_address"
    assert origins["billing_city"] == "unavailable"


def test_in_memory_root_read_has_short_reuse_and_no_payload_repr(setup_extension, monkeypatch):
    case = setup_extension
    capsule = ExtensionRootRead(
        str(case.extension.draft_id),
        case.raw["id"],
        case.extension.chain.revision,
        case.raw,
        checked_at=100,
    )
    monkeypatch.setattr("apps.reservations.extension_contact_source.monotonic", lambda: 105)
    assert capsule.reusable(case.extension)
    assert case.raw["phone"] not in repr(capsule)
    monkeypatch.setattr("apps.reservations.extension_contact_source.monotonic", lambda: 111)
    assert not capsule.reusable(case.extension)


def test_revision_is_submitted_without_a_technical_label(setup_extension):
    from django.urls import reverse

    case = setup_extension
    response = case.client.get(
        reverse("notifications:stay_extension_detail", args=[case.extension.pk])
    )
    assert response.status_code == 200
    body = response.content.decode()
    assert 'name="extension_contact_revision"' in body
    assert 'for="id_extension_contact_revision"' not in body


@pytest.mark.skipif(connection.vendor != "postgresql", reason="Requires real PostgreSQL row locks")
@pytest.mark.django_db(transaction=True)
def test_two_creators_freeze_one_intent_and_one_payload(setup_extension, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor

    from django.db import close_old_connections

    case = setup_extension
    finalize(case)
    ownership(monkeypatch)
    provider = FakeHostaway(case)

    def create():
        close_old_connections()
        try:
            return create_manual_booking_in_hostaway(
                draft_id=case.extension.draft_id, booking_service=service(case, provider)
            ).code
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _: create(), range(2)))
    assert "created" in results
    assert BookingIntent.objects.count() == 1
    assert len(provider.posts) == 1
