"""The billing address asks for what the gateway needs, and no more."""

import pytest

from apps.payments.regions import SAUDI_REGIONS
from apps.reservations.booking_forms import GuestDetailsForm

pytestmark = pytest.mark.django_db

BASE = {
    "guest_first_name": "Guest",
    "guest_last_name": "Example",
    "guest_email": "guest@example.invalid",
    "guest_phone": "+966500000000",
    "billing_city": "Riyadh",
    "billing_country": "SA",
    "special_requests": "",
    "terms_accepted": "on",
    "privacy_accepted": "on",
    "idempotency_key": "x" * 32,
}


def form(**overrides: object) -> GuestDetailsForm:
    return GuestDetailsForm({**BASE, "billing_region_sa": "Riyadh", **overrides})


# --- what is no longer demanded ---------------------------------------------


def test_a_booking_completes_without_a_street_or_a_postcode() -> None:
    submitted = form()

    assert submitted.is_valid(), submitted.errors
    assert submitted.cleaned_data["billing_street1"] == ""
    assert submitted.cleaned_data["billing_postcode"] == ""


def test_the_city_is_still_required() -> None:
    submitted = form(billing_city="")

    assert submitted.is_valid() is False
    assert "billing_city" in submitted.errors


def test_a_postcode_is_still_validated_when_given() -> None:
    submitted = form(billing_postcode="12 34!")

    assert submitted.is_valid() is False
    assert "billing_postcode" in submitted.errors


# --- the Saudi region list --------------------------------------------------


def test_saudi_arabia_has_all_thirteen_regions() -> None:
    assert len(SAUDI_REGIONS) == 13


@pytest.mark.parametrize("region", [value for value, _label in SAUDI_REGIONS])
def test_every_region_is_accepted(region: str) -> None:
    submitted = form(billing_region_sa=region)

    assert submitted.is_valid(), submitted.errors
    assert submitted.cleaned_data["billing_state"] == region


def test_a_saudi_address_needs_one_of_the_two_region_controls() -> None:
    submitted = form(billing_region_sa="", billing_state="")

    assert submitted.is_valid() is False
    assert "billing_region_sa" in submitted.errors


def test_free_text_still_satisfies_a_saudi_address() -> None:
    """Older clients and direct posts must not fail on presentation."""
    submitted = form(billing_region_sa="", billing_state="Riyadh Province")

    assert submitted.is_valid(), submitted.errors
    assert submitted.cleaned_data["billing_state"] == "Riyadh Province"


def test_another_country_keeps_the_free_text_region() -> None:
    submitted = form(
        billing_country="FR",
        billing_region_sa="",
        billing_state="Île-de-France",
        billing_phone=None,
    )

    assert submitted.is_valid(), submitted.errors
    assert submitted.cleaned_data["billing_state"] == "Île-de-France"


def test_a_stale_saudi_region_is_dropped_when_the_country_changes() -> None:
    """Switching country must not leave the previous list value behind."""
    submitted = form(
        billing_country="FR",
        billing_region_sa="Riyadh",
        billing_state="Île-de-France",
    )

    assert submitted.is_valid(), submitted.errors
    assert submitted.cleaned_data["billing_region_sa"] == ""
    assert submitted.cleaned_data["billing_state"] == "Île-de-France"


def test_a_region_outside_the_list_is_refused_by_the_select() -> None:
    submitted = form(billing_region_sa="Atlantis")

    assert submitted.is_valid() is False
    assert "billing_region_sa" in submitted.errors


# --- what the gateway is sent ------------------------------------------------


def test_an_empty_optional_field_is_omitted_rather_than_sent_blank() -> None:
    from decimal import Decimal
    from types import SimpleNamespace

    from apps.payments.hyperpay.service import build_checkout_payload

    intent = SimpleNamespace(
        guest_email="guest@example.invalid",
        guest_first_name="Guest",
        guest_last_name="Example",
        billing_street1="",
        billing_city="Riyadh",
        billing_state="Riyadh",
        billing_postcode="",
        billing_country="SA",
        total_price=Decimal("500.00"),
        currency="SAR",
    )

    payload = build_checkout_payload(intent, merchant_id="probe")

    assert "billing.street1" not in payload
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
        currency="SAR",
    )

    payload = build_checkout_payload(intent, merchant_id="probe")

    assert payload["billing.street1"] == "King Fahd Road 10"
    assert payload["billing.postcode"] == "12345"


# --- the page ----------------------------------------------------------------


def test_the_review_page_offers_both_region_controls() -> None:
    from tests.test_booking_views_admin import owned_client_quote

    client, _quote, reference = owned_client_quote()

    content = client.get(f"/reservations/quotes/{reference}/").content.decode()

    assert 'data-region-group="SA"' in content
    assert 'data-region-group="other"' in content
    assert "data-draft-form" in content
