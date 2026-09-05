"""The billing address asks for what the gateway needs, and no more."""

import pytest
from django.conf import settings
from django.forms import Select, TextInput

from apps.reservations.booking_forms import GuestDetailsForm

pytestmark = pytest.mark.django_db

BASE = {
    "guest_first_name": "Guest",
    "guest_last_name": "Example",
    "guest_email": "guest@example.invalid",
    "guest_phone": "0500000000",
    "billing_street1": "King Fahd Road 10",
    "billing_city": "Riyadh",
    "billing_state": "Riyadh",
    "billing_country": "SA",
    "special_requests": "",
    "terms_accepted": "on",
    "privacy_accepted": "on",
    "idempotency_key": "x" * 32,
}


def form(**overrides: object) -> GuestDetailsForm:
    return GuestDetailsForm({**BASE, **overrides})


# --- what the payment journey actually demands ------------------------------


def test_a_booking_requires_the_street_used_by_three_d_secure() -> None:
    submitted = form(billing_street1="")

    assert submitted.is_valid() is False
    assert "billing_street1" in submitted.errors


def test_a_booking_completes_without_a_postcode() -> None:
    submitted = form(billing_street1="King Fahd Road 10")

    assert submitted.is_valid(), submitted.errors
    assert submitted.cleaned_data["billing_postcode"] == ""


def test_the_city_is_still_required() -> None:
    submitted = form(billing_city="")

    assert submitted.is_valid() is False
    assert "billing_city" in submitted.errors


def test_a_postcode_is_still_validated_when_given() -> None:
    submitted = form(billing_postcode="12 34!")

    assert submitted.is_valid() is False
    assert "billing_postcode" in submitted.errors


# --- direct-entry address fields -------------------------------------------


def test_country_is_a_select_while_city_and_region_are_text_inputs() -> None:
    submitted = GuestDetailsForm()

    assert isinstance(submitted.fields["billing_country"].widget, Select)
    assert isinstance(submitted.fields["billing_city"].widget, TextInput)
    assert isinstance(submitted.fields["billing_state"].widget, TextInput)
    assert "billing_region_sa" not in submitted.fields


def test_selected_country_keeps_its_iso_code_for_the_backend() -> None:
    submitted = form(billing_country="SA")

    assert submitted.is_valid(), submitted.errors
    assert submitted.cleaned_data["billing_country"] == "SA"


def test_country_rejects_a_value_outside_the_iso_choices() -> None:
    submitted = form(billing_country="Saudi Arabia")

    assert submitted.is_valid() is False
    assert "billing_country" in submitted.errors


def test_written_region_is_accepted_for_saudi_arabia() -> None:
    submitted = form(billing_state="Riyadh Province")

    assert submitted.is_valid(), submitted.errors
    assert submitted.cleaned_data["billing_state"] == "Riyadh Province"


def test_another_country_keeps_the_free_text_region() -> None:
    submitted = form(
        billing_country="FR",
        billing_state="Île-de-France",
        billing_phone=None,
    )

    assert submitted.is_valid(), submitted.errors
    assert submitted.cleaned_data["billing_state"] == "Île-de-France"


def test_written_region_is_still_required() -> None:
    submitted = form(billing_state="")

    assert submitted.is_valid() is False
    assert "billing_state" in submitted.errors


# --- what the gateway is sent ------------------------------------------------


def test_an_empty_postcode_is_omitted_rather_than_sent_blank() -> None:
    from decimal import Decimal
    from types import SimpleNamespace

    from apps.payments.hyperpay.service import build_checkout_payload

    intent = SimpleNamespace(
        guest_email="guest@example.invalid",
        guest_first_name="Guest",
        guest_last_name="Example",
        billing_street1="King Fahd Road 10",
        billing_city="Riyadh",
        billing_state="Riyadh",
        billing_postcode="",
        billing_country="SA",
        total_price=Decimal("500.00"),
    )

    payload = build_checkout_payload(
        intent,
        merchant_id="probe",
        amount=Decimal("500.00"),
        currency="SAR",
    )

    assert payload["billing.street1"] == "King Fahd Road 10"
    assert "billing.postcode" not in payload
    assert payload["billing.city"] == "Riyadh"


def test_a_supplied_optional_field_is_sent() -> None:
    from decimal import Decimal
    from types import SimpleNamespace

    from apps.payments.hyperpay.service import build_checkout_payload

    intent = SimpleNamespace(
        guest_email="guest@example.invalid",
        guest_first_name="Guest",
        guest_last_name="Example",
        billing_street1="King Fahd Road 10",
        billing_city="Riyadh",
        billing_state="Riyadh",
        billing_postcode="12345",
        billing_country="SA",
        total_price=Decimal("500.00"),
    )

    payload = build_checkout_payload(
        intent,
        merchant_id="probe",
        amount=Decimal("500.00"),
        currency="SAR",
    )
    assert payload["billing.street1"] == "King Fahd Road 10"
    assert payload["billing.postcode"] == "12345"


# --- the page ----------------------------------------------------------------


def test_the_review_page_uses_direct_entry_address_fields() -> None:
    from tests.test_booking_views_admin import owned_client_quote

    client, _quote, reference = owned_client_quote()
    client.cookies[settings.LANGUAGE_COOKIE_NAME] = "en"

    content = client.get(f"/reservations/quotes/{reference}/").content.decode()

    assert '<select name="billing_country"' in content
    assert 'type="text" name="billing_city"' in content
    assert 'type="text" name="billing_state"' in content
    assert 'name="billing_region_sa"' not in content
    assert "data-country-select" in content
    assert "data-draft-form" in content
    assert "billing_street1" in content
    assert "Street address</label>" in content
