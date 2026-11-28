"""Synthetic GET/form integration; no real guest/provider or create request."""

from unittest.mock import Mock

import pytest
from django.core import signing
from django.urls import reverse

from apps.reservations.extension_contact_source import (
    BILLING_FIELDS,
    GUEST_FIELDS,
    contact_values,
    field_provenance,
    merge_contact_fields,
    provider_context,
    root_contact_source,
)
from apps.reservations.models import StayExtension
from apps.reservations.stay_extension_communications import context
from apps.reservations.stay_extension_views import TICKET_SALT
from apps.reservations.stay_extensions import start_extension
from tests.test_stay_extensions import create_child

pytest_plugins = ["tests.test_stay_extensions"]

pytestmark = pytest.mark.django_db


def address(raw):
    return {
        **raw,
        "guestAddress": "10 Test Street",
        "guestCity": "Riyadh",
        "guestState": "Riyadh",
        "guestZipCode": "12345",
        "guestCountry": "SA",
    }


def submitted(values):
    return {
        **{key: values[key] for key in GUEST_FIELDS},
        "billing_address": {key: values[key] for key in BILLING_FIELDS},
    }


def test_actual_selector_uses_fresh_unchanged_fields_and_preserves_explicit_edit(
    setup_extension, monkeypatch
):
    case = setup_extension
    raw = address(case.raw)
    monkeypatch.setattr("apps.reservations.stay_extension_views.read_source", lambda *a: dict(raw))
    url = reverse("notifications:stay_extension_sources")
    page = case.client.get(url + f"?source={raw['id']}")
    assert page.status_code == 200
    form = page.context["form"]
    ticket = form.initial["source_ticket"]
    claims = signing.loads(ticket, salt=TICKET_SALT)
    assert "guest_phone" not in claims
    assert raw["phone"] not in str(claims)
    data = {key: form[key].value() for key in form.fields if form[key].value() is not None}
    data.update(guest_email="explicit@example.invalid", guest_phone_country="SA")
    raw.update(phone="+966551234567", guestLastName="حديث", guestAddress="20 Current Road")
    case.extension.delete()  # The selector now creates a new synthetic draft.
    original = start_extension

    def create(**kwargs):
        return original(**kwargs, availability_service=case.availability)

    monkeypatch.setattr("apps.reservations.stay_extension_views.start_extension", create)
    response = case.client.post(url, data)
    assert response.status_code == 302
    extension = StayExtension.objects.get()
    assert extension.draft.guest_phone == raw["phone"]
    assert extension.draft.guest_last_name == "حديث"
    assert extension.draft.guest_email == "explicit@example.invalid"
    assert extension.draft.billing_address["billing_street1"] == "20 Current Road"
    assert extension.reservation_id is None


@pytest.mark.parametrize("field", GUEST_FIELDS + BILLING_FIELDS)
def test_field_merge_is_independent_and_explicit_edits_survive(setup_extension, field):
    raw = address(setup_extension.raw)
    old = contact_values(raw)
    data = submitted(old)
    if field in GUEST_FIELDS:
        data[field] = "explicit"
    else:
        data["billing_address"][field] = "explicit"
    baseline = field_provenance(old, user_id=1, root_id=raw["id"])
    merged = merge_contact_fields(data, raw, baseline=baseline, user_id=1, root_id=raw["id"])
    assert (merged if field in GUEST_FIELDS else merged["billing_address"])[field] == "explicit"


def test_phone_formatting_is_not_an_explicit_override(setup_extension):
    raw = address(setup_extension.raw)
    old = contact_values(raw)
    data = submitted(old)
    data["guest_phone"] = "00966500000000"
    baseline = field_provenance(old, user_id=1, root_id=raw["id"])
    raw["phone"] = "+966551234567"
    result = merge_contact_fields(data, raw, baseline=baseline, user_id=1, root_id=raw["id"])
    assert result["guest_phone"] == raw["phone"]


def test_automatic_name_cleanup_does_not_become_an_explicit_override(setup_extension):
    raw = address(setup_extension.raw)
    raw["guestFirstName"] = "<b>اسم</b>"
    old = contact_values(raw)
    data = submitted(old)
    data["guest_first_name"] = "اسم"
    baseline = field_provenance(old, user_id=1, root_id=raw["id"])
    raw["guestFirstName"] = "حديث"
    result = merge_contact_fields(data, raw, baseline=baseline, user_id=1, root_id=raw["id"])
    assert result["guest_first_name"] == "حديث"


def test_provenance_is_required_and_root_bound(setup_extension):
    raw = address(setup_extension.raw)
    values = contact_values(raw)
    with pytest.raises(ValueError, match="provenance_required"):
        merge_contact_fields(submitted(values), raw, baseline=None, user_id=1, root_id=raw["id"])
    with pytest.raises(ValueError, match="root_mismatch"):
        merge_contact_fields(
            submitted(values),
            raw,
            baseline=field_provenance(values, user_id=1, root_id=raw["id"]),
            user_id=1,
            root_id=raw["id"] + 1,
        )


def test_root_selection_reuses_get_and_child_selection_reads_root(setup_extension, monkeypatch):
    case = setup_extension
    reader = Mock(return_value=dict(case.raw))
    monkeypatch.setattr("apps.reservations.extension_contact_source.read_source", reader)
    assert root_contact_source(case.raw) == case.raw
    reader.assert_not_called()
    result, _, _ = create_child(case, monkeypatch)
    reader.reset_mock()
    child = {
        **case.raw,
        "id": result.reservation.hostaway_reservation_id,
        "arrivalDate": result.reservation.check_in.isoformat(),
        "departureDate": result.reservation.check_out.isoformat(),
        "phone": "+966551111111",
    }
    case.raw["phone"] = "+966552222222"
    reader.return_value = dict(case.raw)
    assert root_contact_source(child)["phone"] == "+966552222222"
    reader.assert_called_once_with(case.raw["id"])
    reader.return_value = {**case.raw, "id": 99999}
    with pytest.raises(ValueError, match="root_changed"):
        root_contact_source(child)


def test_context_evidence_uses_existing_gets_and_exact_child_status(setup_extension, monkeypatch):
    case = setup_extension
    result, provider, _ = create_child(case, monkeypatch)
    reservation = result.reservation
    child = {
        **case.raw,
        "id": reservation.hostaway_reservation_id,
        "arrivalDate": reservation.check_in.isoformat(),
        "departureDate": reservation.check_out.isoformat(),
        "channelId": reservation.channel_id,
        "totalPrice": str(reservation.total_price),
        "guestFirstName": "Current",
        "phone": "+966551234567",
        "updatedOn": "2026-11-28T10:22:33Z",
    }
    reader = Mock(side_effect=lambda key, **kw: dict(case.raw) if key == case.raw["id"] else child)
    monkeypatch.setattr("apps.reservations.stay_extension_communications.read_source", reader)
    values = context(case.extension)
    assert reader.call_count == 2
    evidence = values["provider_context_v2"]
    assert evidence["contract"] == "site01-extension-provider-context-2"
    assert evidence["child"]["hostaway_reservation_id"] == child["id"]
    assert evidence["child"]["status"] == "confirmed"
    assert evidence["child"]["provider_status"] == "new"
    assert evidence["child"]["provider_updated_at"] == "2026-11-28T10:22:33+00:00"
    assert evidence["child"]["contact"]["guest_first_name"] == "Current"
    assert evidence["child"]["contact"]["guest_phone"] == child["phone"]
    assert evidence["root"]["provider_updated_at"] is None
    assert len(provider.posts) == 1
    assert context(case.extension, verify_live=False)["provider_context_v2"] is None
    child["id"] = 99999
    assert context(case.extension)["provider_context_v2"] is None


def test_provider_context_missing_timestamp_never_uses_clock(setup_extension):
    raw = dict(setup_extension.raw)
    evidence = provider_context(raw)
    assert evidence["provider_updated_at"] is None
    assert "sensitive_unused" not in str(evidence)


@pytest.mark.parametrize("status", ["cancelled", "declined", "expired"])
def test_closed_subtypes_are_not_conflated(setup_extension, status):
    evidence = provider_context({**setup_extension.raw, "status": status})
    assert evidence["provider_status"] == status
    assert evidence["status"] == status


def test_changed_root_blocks_without_draft_fallback(setup_extension, monkeypatch):
    case = setup_extension
    create_child(case, monkeypatch)
    changed = {**case.raw, "departureDate": case.extension.draft.check_out.isoformat()}
    with pytest.raises(ValueError, match="root_changed"):
        root_contact_source(changed)


def test_explicit_provider_address_clear_does_not_restore_old_value(setup_extension, monkeypatch):
    monkeypatch.setattr(
        "apps.reservations.extension_contact_source.billing_initial",
        lambda raw: {"billing_street1": "Old saved address"},
    )
    raw = {**setup_extension.raw, "guestAddress": ""}
    assert contact_values(raw)["billing_street1"] == ""


def test_naive_provider_timestamp_is_preserved_without_inventing_zone(setup_extension):
    evidence = provider_context({**setup_extension.raw, "updatedOn": "2026-11-28 10:22:33"})
    assert evidence["provider_updated_at"] is None
    assert evidence["provider_updated_at_raw"] == "2026-11-28 10:22:33"
    assert evidence["provider_updated_at_field"] == "updatedOn"
