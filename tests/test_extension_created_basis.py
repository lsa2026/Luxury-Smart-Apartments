"""Existing authenticated GET only; fake provider, no queue/claim or sender."""

import json
import os
from datetime import UTC
from pathlib import Path
from unittest.mock import Mock

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from apps.reservations.extension_created_basis import created_booking_update_basis
from apps.reservations.models import StayExtensionEvent
from apps.reservations.stay_extension_communications import capability
from tests import test_stay_extensions as original

setup_extension = original.setup_extension
pytestmark = pytest.mark.django_db
BASIS_KEYS = {
    "contract_version",
    "extension_id",
    "chain_id",
    "creation_event_id",
    "creation_event_revision",
    "creation_occurred_at",
    "root_reservation_key",
    "child_reservation_key",
    "listing_map_id",
    "language",
    "values",
}
VALUE_KEYS = [
    "guest_name",
    "booking_reference",
    "listing_name",
    "arrival_date",
    "check_in_time",
    "departure_date",
    "check_out_time",
    "timezone_display",
]


def setup(case, monkeypatch, language="ar"):
    case.property.name_en = "Synthetic Apartment"
    case.property.name_fr = "Appartement fictif"
    case.property.save(update_fields=["name_en", "name_fr"])
    result, provider, _ = original.create_child(case, monkeypatch)
    assert result.code == "created"
    event = case.extension.events.get(kind="stay_extension.created_unpaid")
    child = {
        **case.raw,
        **provider.posts[0],
        "id": result.reservation.hostaway_reservation_id,
        "status": result.reservation.normalized_status,
        "phone": "+966551234567",
        "guestFirstName": "Current",
        "guestLastName": "Child",
        "guestLocale": language,
        "updatedOn": "2026-11-28T08:00:00Z",
    }
    reader = Mock(side_effect=lambda key, **kw: dict(case.raw) if key == case.raw["id"] else child)
    monkeypatch.setattr("apps.reservations.stay_extension_communications.read_source", reader)
    return event, child, reader


def get(case, event):
    return case.client.get(
        reverse("reservations:stay_extension_context", args=[case.extension.pk]),
        HTTP_X_LSA_EXTENSION_TOKEN=capability(case.extension, event=event),
    )


@pytest.mark.parametrize("language", ["ar", "en", "fr"])
def test_existing_get_has_verified_created_child_values_without_payment(
    setup_extension, monkeypatch, language
):
    case = setup_extension
    event, child, reader = setup(case, monkeypatch, language)
    with CaptureQueriesContext(connection) as queries:
        response = get(case, event)
    assert response.status_code == 200
    data = response.json()
    basis = data["created_booking_update_basis"]
    assert set(basis) == BASIS_KEYS and list(basis["values"]) == VALUE_KEYS
    assert basis["creation_event_id"] == data["source_event_id"] == str(event.pk)
    assert basis["creation_event_revision"] == str(data["source_event_revision"])
    assert basis["creation_occurred_at"] == event.created_at.astimezone(UTC).isoformat(
        timespec="microseconds"
    ).replace("+00:00", "Z")
    assert basis["child_reservation_key"] == str(child["id"])
    assert basis["root_reservation_key"] == str(case.raw["id"])
    assert basis["listing_map_id"] == str(case.property.hostaway_listing_map_id)
    assert basis["language"] == language
    assert basis["values"]["guest_name"] == "Current Child"
    assert basis["values"]["booking_reference"] == str(child["id"])
    assert basis["values"]["arrival_date"] == case.extension.draft.check_in.isoformat()
    assert basis["values"]["departure_date"] == case.extension.draft.check_out.isoformat()
    assert basis["values"]["check_in_time"] == "15:00"
    assert basis["values"]["check_out_time"] == "12:00"
    assert data["booking_verified"] is True and data["payment_verified"] is False
    assert str(data["terminal_hostaway_reservation_id"]) != str(data["new_hostaway_reservation_id"])
    assert data["provider_context_v2"]["child"]["contact"]["guest_phone"] == child["phone"]
    assert "recipient_generation" not in json.dumps(basis)
    assert reader.call_count == 2
    assert not any(
        q["sql"].lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE")) for q in queries
    )
    directory = os.environ.get("EXTENSION_BASIS_FIXTURE_DIR")
    if directory:
        output = Path(directory)
        output.mkdir(parents=True, exist_ok=True)
        fixture = {
            "synthetic_only": True,
            "origin": "actual Django GET with fake provider",
            "response": data,
        }
        output.joinpath(f"created-unpaid-{language}.json").write_text(
            json.dumps(fixture, ensure_ascii=False, indent=2), encoding="utf-8"
        )


def test_creation_identity_survives_payment_and_personal_loop(setup_extension, monkeypatch):
    case = setup_extension
    event, child, _ = setup(case, monkeypatch)
    before = get(case, event).json()
    original.pay(case)
    paid = get(case, event).json()
    child.update(guestFirstName="Updated", guestEmail="updated@example.invalid")
    personal = get(case, event).json()
    assert paid["revision"] > before["revision"]
    for data in (paid, personal):
        for key in (
            "creation_event_id",
            "creation_event_revision",
            "creation_occurred_at",
            "extension_id",
            "chain_id",
            "root_reservation_key",
            "child_reservation_key",
        ):
            assert (
                data["created_booking_update_basis"][key]
                == before["created_booking_update_basis"][key]
            )
    assert personal["created_booking_update_basis"]["values"]["guest_name"] == "Updated Child"
    assert child["phone"] == personal["provider_context_v2"]["child"]["contact"]["guest_phone"]
    directory = os.environ.get("EXTENSION_BASIS_FIXTURE_DIR")
    if directory:
        Path(directory, "creation-payment-personal-loop.json").write_text(
            json.dumps(
                {
                    "synthetic_only": True,
                    "created_unpaid": before,
                    "after_payment": paid,
                    "after_personal_change": personal,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )


@pytest.mark.parametrize(
    "kind", ["stay_extension.changed", "stay_extension.activated", "stay_extension.review_required"]
)
def test_other_event_kinds_cannot_impersonate_creation(setup_extension, monkeypatch, kind):
    case = setup_extension
    setup(case, monkeypatch)
    later = StayExtensionEvent.objects.create(
        extension=case.extension, revision=case.extension.chain.revision + 10, kind=kind
    )
    data = get(case, later).json()
    assert data["source_event_kind"] == kind
    assert data["created_booking_update_basis"] is None


def test_first_is_revision_not_arbitrary_equal_timestamp_uuid(setup_extension, monkeypatch):
    case = setup_extension
    event, _, _ = setup(case, monkeypatch)
    later = StayExtensionEvent.objects.create(
        extension=case.extension,
        revision=event.revision + 10,
        kind=event.kind,
        created_at=event.created_at,
    )
    assert get(case, event).json()["created_booking_update_basis"] is not None
    assert get(case, later).json()["created_booking_update_basis"] is None


def test_ownership_capability_does_not_claim_creation(setup_extension):
    case = setup_extension
    data = get(case, None).json()
    assert data["capability_purpose"] == "ownership"
    assert data["created_booking_update_basis"] is None


def test_cancelled_or_unverified_source_does_not_authorize_created_notice(
    setup_extension, monkeypatch
):
    case = setup_extension
    event, _, _ = setup(case, monkeypatch)
    case.raw["status"] = "cancelled"
    data = get(case, event).json()
    assert data["source_event_id"] == str(event.pk)
    assert data["communication_ready"] is False
    assert data["created_booking_update_basis"] is None


def test_event_from_another_extension_is_rejected(setup_extension):
    case = setup_extension
    event = Mock(extension_id="foreign")
    with pytest.raises(ValueError, match="scope_mismatch"):
        created_booking_update_basis(case.extension, event, {})
