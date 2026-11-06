"""Synthetic morning input boundaries. All external traffic must remain mocked."""

from unittest.mock import patch
from urllib.parse import urlsplit

import pytest
from django.test import Client
from django.urls import reverse

from apps.payments.morning_payments import create_checkout, public_url
from apps.payments.payment_link_views import PaymentLinkBillingForm
from apps.reservations.morning_billing import MorningBillingForm, MorningPaymentForm
from apps.reservations.morning_validation import MorningGuestForm, MorningInputError, region_code
from tests.test_hyperpay import HyperPayStub
from tests.test_morning_stays import consent, create, property_and_remote
from tests.test_morning_stays import isolated as isolated  # noqa: F401
from tests.test_reservation_payment_links import billing, form_data

pytestmark = pytest.mark.django_db


def test_ordinary_checkout_contract_is_not_changed():
    values = billing() | {"billing_postcode": "AbulazizAlsulami"}
    assert PaymentLinkBillingForm(form_data() | values).is_valid()
    assert not MorningBillingForm(values).is_valid()


@pytest.mark.parametrize(
    "name,value,fragment",
    [
        ("billing_postcode", "AbulazizAlsulami", "10"),
        ("billing_postcode", "ROAD", "5"),
        ("billing_postcode", "1234", "5"),
        ("billing_postcode", "١٣٢٢١", "5"),
        ("billing_postcode", "1322<", "5"),
        ("billing_city", "R", "2"),
        ("billing_city", "R" * 46, "45"),
        ("billing_street1", "R" * 51, "50"),
        ("billing_street1", "<b></b>", "وسوم"),
        ("billing_state", "Unknown Province", "01"),
        ("billing_country", "ZZ", "دولة"),
    ],
)
def test_invalid_billing_is_rejected_with_specific_field_error(name, value, fragment):
    form = MorningBillingForm(billing() | {name: value})
    assert not form.is_valid()
    assert fragment in str(form.errors[name])


def test_valid_billing_boundaries_and_known_regions_preserve_original_values():
    values = billing() | {"billing_city": "R" * 45, "billing_street1": "S" * 50}
    form = MorningBillingForm(values)
    assert form.is_valid(), form.errors
    assert form.cleaned_data == values
    for name in ["الرياض", "Riyadh Province", "SA-01", "01"]:
        assert region_code(name, "SA") == "01"
    assert region_code("US-NY", "US") == "NY"
    us = MorningBillingForm(
        values | {"billing_country": "US", "billing_state": "NY", "billing_postcode": "10001-1234"}
    )
    assert us.is_valid(), us.errors


@pytest.mark.parametrize(
    "name,value,fragment",
    [
        ("guest_name", "Single", "العائلة"),
        ("guest_name", "A" * 51 + " Guest", "50"),
        ("guest_name", "Guest " + "A" * 51, "50"),
        ("guest_name", "123 456", "أرقام"),
        ("guest_email", "not-an-email", "البريد"),
        ("guest_email", "a" * 117 + "@example.test", "128"),
        ("guest_phone", "0501234567", "+"),
        ("guest_phone", "+966123", "غير صحيح"),
    ],
)
def test_guest_field_contract(name, value, fragment):
    data = {
        "guest_name": "Synthetic Guest",
        "guest_email": "synthetic@example.test",
        "guest_phone": "+16502530000",
    }
    form = MorningGuestForm(data | {name: value})
    assert not form.is_valid()
    assert fragment in str(form.errors[name])


def test_invalid_request_never_checks_remote_or_creates_payment_attempt():
    stay = create()
    with (
        patch("apps.payments.morning_payments.verify_window") as availability,
        patch("apps.payments.morning_payments.HyperPayService") as gateway,
    ):
        with pytest.raises(MorningInputError) as error:
            create_checkout(
                stay.pk,
                billing_data=billing() | {"billing_postcode": "AbulazizAlsulami"},
                acceptance=consent(),
            )
    assert "billing_postcode" in error.value.message_dict
    availability.assert_not_called()
    gateway.assert_not_called()
    assert stay.attempts.count() == 0
    stay.refresh_from_db()
    assert stay.status == "open"


def test_legacy_bad_address_can_be_corrected_before_new_checkout():
    prop, remote = property_and_remote()
    stay = create(prop, remote)
    stay.billing_address = billing() | {"billing_postcode": "AbulazizAlsulami"}
    stay.save()
    path = urlsplit(public_url(stay)).path
    page = Client().get(path)
    assert "العنوان المحفوظ يحتاج تصحيحًا" in page.content.decode()
    assert b'value="AbulazizAlsulami"' in page.content
    with patch("apps.payments.morning_views.create_checkout") as gateway:
        response = Client().post(path, form_data() | stay.billing_address)
    assert response.status_code == 200
    assert "الرمز البريدي" in response.content.decode()
    gateway.assert_not_called()
    gateway = HyperPayStub()
    create_checkout(
        stay.pk, billing_data=billing(), acceptance=consent(), client=gateway, hostaway=remote
    )
    assert gateway.payload["billing.postcode"] == "12345"
    assert gateway.payload["billing.state"] == "01"
    assert gateway.payload["customer.phone"] == "+1-6502530000"
    stay.refresh_from_db()
    assert stay.billing_address == billing()


def test_guest_and_admin_show_persistent_hints_and_field_errors():
    stay = create()
    guest = Client().get(urlsplit(public_url(stay)).path)
    html = guest.content.decode()
    assert "morning-validation.js" in html
    assert "id_billing_postcode_rule" in html
    assert "id_billing_postcode_error" in html
    assert 'maxlength="10"' in html
    assert "5 أرقام" in html
    admin = Client()
    admin.force_login(stay.created_by)
    page = admin.get(reverse("notifications:morning_create"))
    assert b"id_guest_name_rule" in page.content
    assert b"id_guest_email_rule" in page.content
    response = admin.post(
        reverse("notifications:morning_detail", args=[stay.pk]),
        {"action": "save_billing", **billing(), "billing_postcode": "AbulazizAlsulami"},
    )
    assert response.status_code == 200
    stay.refresh_from_db()
    assert not stay.billing_address
    assert not stay.attempts.exists()
    assert MorningPaymentForm(form_data()).is_valid()
