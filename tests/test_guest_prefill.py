"""Saved guest recovery: synthetic data, read-only queries and fake Hostaway only."""

import os
from datetime import timedelta
from pathlib import Path
from unittest.mock import Mock
from uuid import uuid4

import pytest
from django.contrib.auth import get_user_model
from django.db import connection
from django.test import Client
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import CustomerProfile, VerifiedPhone
from apps.reservations.models import DayUseStay, MorningStay
from apps.reservations.services import guest_prefill
from apps.reservations.services.guest_classification import contact_keys
from tests.test_guest_classification import factory as _factory
from tests.test_guest_classification import isolated as _isolated
from tests.test_guest_classification import owner_client as _owner_client
from tests.test_reservation_payment_links import billing

pytestmark = pytest.mark.django_db
EMAIL, PHONE = "returning@example.invalid", "+16502530000"
factory, isolated, owner_client = _factory, _isolated, _owner_client


def saved(factory, **changes):
    booking = factory(EMAIL, PHONE, **changes)
    intent = booking.booking_intent
    intent.guest_first_name, intent.guest_last_name = "Synthetic", "Guest"
    for key, value in billing().items():
        setattr(intent, key, value)
    intent.save()
    return booking


def snapshot(*rows):
    return {"history": [dict(id=ref, email_key=e, phone_key=p) for ref, e, p in rows]}


def source(**changes):
    raw = {
        "id": 99123,
        "guestName": "Synthetic Guest",
        "guestEmail": EMAIL,
        "phone": PHONE,
        "guestLocale": "fr-FR",
        "guestAddress": "King Road 10",
        "guestCity": "Riyadh",
        "guestState": "Riyadh",
        "guestCountry": "SA",
        "guestZipCode": "12345",
        "departureDate": "2026-11-01",
        "secret": "must-never-be-returned",
        "totalPrice": 900,
    }
    return {"status": "success", "result": raw | changes}


def test_local_restoration_is_read_only_and_allowlisted(factory):
    booking = saved(factory)
    before = {
        field.attname: getattr(booking.booking_intent, field.attname)
        for field in booking.booking_intent._meta.concrete_fields
    }
    with CaptureQueriesContext(connection) as queries:
        result = guest_prefill.lookup(EMAIL.upper(), PHONE, client=Mock())
    assert result["status"] == "found"
    candidate = result["candidates"][0]
    assert candidate["source_ref"] == f"booking:{booking.pk}"
    assert candidate["fields"] == dict(
        guest_first_name="Synthetic",
        guest_last_name="Guest",
        guest_name="Synthetic Guest",
        guest_email=EMAIL,
        guest_phone=PHONE,
        guest_phone_country="US",
        guest_language="ar",
        **billing(),
    )
    assert candidate["address_complete"] and candidate["review_required"]
    assert all(q["sql"].lstrip().upper().startswith("SELECT") for q in queries)
    booking.booking_intent.refresh_from_db()
    assert {key: getattr(booking.booking_intent, key) for key in before} == before
    assert not VerifiedPhone.objects.exists()


def test_both_contacts_must_match_same_source_and_phone_is_international(factory):
    saved(factory)
    factory("different@example.invalid", "+33650253000")
    assert guest_prefill.lookup(EMAIL, "+33650253000")["candidates"] == []
    assert guest_prefill.lookup("different@example.invalid", PHONE)["candidates"] == []
    assert guest_prefill.lookup("", "+16502530000")["status"] == "found"
    assert guest_prefill.lookup(EMAIL, "")["status"] == "found"


@pytest.mark.parametrize("change", [{"is_test": True}, {"normalized_status": "cancelled"}])
def test_test_and_cancelled_local_sources_are_excluded(factory, change):
    saved(factory, **change)
    assert guest_prefill.lookup(EMAIL, PHONE)["candidates"] == []


def test_multiple_sources_remain_separate_and_partial_address_is_not_fabricated(factory):
    first, second = saved(factory), saved(factory)
    intent = second.booking_intent
    for name in billing():
        setattr(intent, name, "Not provided" if name != "billing_country" else "")
    intent.save()
    rows = guest_prefill.lookup(EMAIL, PHONE)["candidates"]
    assert len(rows) == 2
    assert rows[0]["source_ref"] == f"booking:{first.pk}"
    assert rows[0]["address_complete"]
    assert not rows[1]["address_available"]
    assert all(not rows[1]["fields"][name] for name in billing())


def test_profile_residence_is_labelled_and_never_verifies_the_phone():
    user = get_user_model().objects.create_user(
        username="saved-profile", email=EMAIL, first_name="Synthetic", last_name="Guest"
    )
    profile, _ = CustomerProfile.objects.get_or_create(user=user)
    profile.phone = PHONE
    profile.residence_address_line1 = "Saved Street 1"
    profile.residence_city = "Riyadh"
    profile.residence_country = "SA"
    profile.save()
    item = guest_prefill.lookup(EMAIL, PHONE)["candidates"][0]
    assert item["address_kind"] == "residence" and item["address_available"]
    assert not item["address_complete"]
    assert item["fields"]["billing_postcode"] == ""
    assert not VerifiedPhone.objects.exists()


def test_directory_requires_same_source_and_fetches_only_one_matching_id():
    e, p = contact_keys(EMAIL, PHONE)
    other_e, other_p = contact_keys("other@example.invalid", "+33612345678")
    remote = Mock()
    remote.get_reservation_invoice_data.return_value = source()
    history = snapshot((99123, e, p), (99124, e, other_p), (99125, other_e, p))
    result = guest_prefill.lookup(EMAIL, PHONE, snapshot=history, client=remote)
    remote.get_reservation_invoice_data.assert_called_once_with(99123)
    item = result["candidates"][0]
    assert item["address_kind"] == "contact" and item["fields"]["guest_language"] == "fr"
    assert set(item["fields"]) == set(guest_prefill.ADDRESS_FIELDS + guest_prefill.CONTACT_FIELDS)
    assert "must-never-be-returned" not in str(result)
    remote.invoice_search_page.assert_not_called()


@pytest.mark.parametrize(
    "change", [{"id": 99124}, {"guestEmail": "other@example.invalid"}, {"phone": "+33612345678"}]
)
def test_hostaway_identity_is_rechecked_before_any_details_are_returned(change):
    e, p = contact_keys(EMAIL, PHONE)
    remote = Mock()
    remote.get_reservation_invoice_data.return_value = source(**change)
    result = guest_prefill.lookup(EMAIL, PHONE, snapshot=snapshot((99123, e, p)), client=remote)
    assert result["candidates"] == []


def test_complete_local_billing_never_requires_hostaway(factory):
    saved(factory)
    e, p = contact_keys(EMAIL, PHONE)
    remote = Mock()
    assert guest_prefill.lookup(EMAIL, PHONE, snapshot=snapshot((99123, e, p)), client=remote)[
        "candidates"
    ]
    remote.get_reservation_invoice_data.assert_not_called()


def test_provider_failure_keeps_local_fields_available_without_exposing_exception(factory):
    booking = saved(factory)
    booking.booking_intent.billing_postcode = ""
    booking.booking_intent.save()
    remote = Mock()
    remote.get_reservation_invoice_data.side_effect = RuntimeError("private-provider-content")
    e, p = contact_keys(EMAIL, PHONE)
    result = guest_prefill.lookup(EMAIL, PHONE, snapshot=snapshot((99123, e, p)), client=remote)
    assert result["status"] == "found" and result["remote_unavailable"]
    assert "private-provider-content" not in str(result)


def post(client, **changes):
    return client.post(
        reverse("notifications:guest_classification_check"),
        {
            "action": "lookup_saved_guest",
            "email": EMAIL,
            "phone": PHONE,
            "country": "US",
        }
        | changes,
    )


def test_admin_endpoint_has_private_headers_and_does_not_start_a_directory_scan(
    factory, owner_client, isolated
):
    saved(factory)
    response = post(owner_client)
    assert response.status_code == 200 and response.json()["status"] == "found"
    assert {"private", "no-store"} <= set(response["Cache-Control"].split(", "))
    assert response["Referrer-Policy"] == "no-referrer"
    assert not isolated


def test_lookup_requires_operations_session_post_and_csrf(owner_client):
    url = reverse("notifications:guest_classification_check")
    assert Client().post(url, {"action": "lookup_saved_guest"}).status_code in {302, 403}
    ordinary = Client()
    ordinary.force_login(get_user_model().objects.create_user(username="ordinary"))
    assert post(ordinary).status_code == 403
    assert owner_client.get(url).status_code == 405
    strict = Client(enforce_csrf_checks=True)
    strict.cookies = owner_client.cookies
    assert post(strict).status_code == 403


@pytest.mark.parametrize(
    "change",
    [{"phone": "invalid"}, {"email": "bad"}, {"phone": "", "email": ""}, {"reservation_id": "123"}],
)
def test_invalid_or_extra_inputs_never_run_lookup(owner_client, monkeypatch, change):
    lookup = Mock(side_effect=AssertionError("must not lookup"))
    monkeypatch.setattr(guest_prefill, "lookup", lookup)
    assert post(owner_client, **change).status_code == 400
    lookup.assert_not_called()


@pytest.mark.parametrize(
    "model,proof_key", [(MorningStay, "hostaway_morning"), (DayUseStay, "hostaway_day_use")]
)
def test_same_day_sources_require_actual_booking_and_keep_address(
    model, proof_key, factory, owner_client
):
    booking = factory()
    user = get_user_model().objects.get(username="classification-owner")
    now = timezone.now()
    inventory = (
        {
            "inventory_arrival_date": now.date(),
            "inventory_departure_date": (now + timedelta(days=1)).date(),
        }
        if model is DayUseStay
        else {}
    )
    stay = model.objects.create(
        request_id=uuid4(),
        property=booking.property,
        starts_at=now,
        ends_at=now + timedelta(hours=4),
        expires_at=now + timedelta(hours=1),
        availability_checked_at=now,
        created_by=user,
        guest_name="Synthetic Guest",
        guest_phone=PHONE,
        guest_email=EMAIL,
        amount_sar=100,
        environment="production",
        billing_address=billing(),
        **inventory,
    )
    assert guest_prefill.lookup(EMAIL, PHONE)["candidates"] == []
    model.objects.filter(pk=stay.pk).update(
        availability_evidence={
            "creation_mode": "hostaway_first_v2",
            proof_key: {"state": "confirmed"},
        }
    )
    result = guest_prefill.lookup(EMAIL, PHONE)
    assert result["candidates"][0]["address_complete"]
    assert result["candidates"][0]["fields"]["billing_street1"] == billing()["billing_street1"]


def test_render_recovery_on_manual_booking_form(tmp_path):
    from tests.test_contact_layout import _standalone
    from tests.test_manual_guest_language import make_draft

    draft = make_draft()
    client = Client()
    client.force_login(draft.created_by)
    page = client.get(reverse("notifications:manual_booking_detail", args=[draft.pk]))
    assert page.status_code == 200
    html = page.content.decode()
    assert "data-load-saved-guest hidden" in html
    assert "data-apply-saved-address" in html and "js/admin-guest-prefill.js" in html
    output = Path(os.environ.get("LSA_CONTACT_VISUAL_DIR", tmp_path))
    output.mkdir(parents=True, exist_ok=True)
    (output / "guest-prefill.html").write_text(_standalone(html), encoding="utf-8")
