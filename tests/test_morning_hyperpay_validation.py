"""Morning-only HyperPay billing checks: synthetic inputs, no live charges."""

from unittest.mock import patch
from urllib.parse import urlsplit

import pytest
from django.test import Client
from django.urls import reverse

from apps.payments.hyperpay.exceptions import HyperPayCheckoutError
from apps.payments.models import MorningPaymentAttempt
from apps.payments.morning_payments import create_checkout, public_url
from apps.reservations.morning_billing import MorningBillingForm, MorningGuestBillingForm
from apps.reservations.morning_views import MorningStayForm
from tests.test_morning_stays import consent, create, property_and_remote, window
from tests.test_morning_stays import isolated as isolated
from tests.test_reservation_payment_links import billing, form_data

pytestmark = pytest.mark.django_db


@pytest.mark.parametrize(
    "field,value",
    [
        ("billing_postcode", "AbulazizAlsulami"),
        ("billing_postcode", "AbulazizA"),
        ("billing_postcode", "1234"),
        ("billing_postcode", "123456"),
        ("billing_postcode", "١٢٣٤٥"),
        ("billing_street1", "x"),
        ("billing_street1", "x" * 51),
        ("billing_city", "R"),
        ("billing_city", "R" * 46),
        ("billing_state", "R" * 51),
    ],
)
def test_rejects_invalid_sa_fields_with_a_field_specific_error(field, value):
    form = MorningBillingForm(billing() | {field: value})
    assert not form.is_valid()
    assert field in form.errors
    assert form.errors[field]
    assert "billing_country" not in form.errors


def test_sa_and_international_postcodes_and_all_hyperpay_boundaries():
    assert MorningBillingForm(billing()).is_valid()
    assert MorningBillingForm(billing() | {
        "billing_country": "GB", "billing_postcode": "SW1A 1AA",
    }).is_valid()
    assert MorningBillingForm(billing() | {
        "billing_country": "US", "billing_postcode": "12345-6789",
    }).is_valid()
    assert not MorningBillingForm(billing() | {
        "billing_country": "GB", "billing_postcode": "abc_def",
    }).is_valid()
    assert not MorningBillingForm(billing() | {
        "billing_country": "US", "billing_postcode": "12345678901",
    }).is_valid()
    assert not MorningBillingForm(billing() | {"billing_country": "XX"}).is_valid()


def test_guest_form_keeps_required_guest_consent_and_feedback():
    form = MorningGuestBillingForm(form_data())
    assert form.is_valid(), form.errors
    assert not MorningGuestBillingForm(billing()).is_valid()
    invalid = MorningGuestBillingForm(
        form_data() | {"billing_postcode": "AbulazizAlsulami"}
    )
    assert not invalid.is_valid()
    assert invalid.errors["billing_postcode"]
    assert "id_billing_postcode_rule" == invalid["billing_postcode"].field.widget.attrs["aria-describedby"]


def test_guest_invalid_address_never_invokes_checkout_or_payment_provider():
    stay = create()
    client = Client()
    url = urlsplit(public_url(stay)).path
    with patch("apps.payments.morning_views.create_checkout") as checkout:
        response = client.post(
            url, form_data() | {"billing_postcode": "AbulazizAlsulami"}
        )
    assert response.status_code == 200
    assert "الرمز البريدي".encode() in response.content
    assert "يرجى تصحيح الحقول".encode() in response.content
    checkout.assert_not_called()
    assert not MorningPaymentAttempt.objects.exists()
    assert b"morning-billing-validation.js" in response.content


def test_saved_invalid_admin_address_blocks_checkout_and_shows_reason():
    stay = create()
    stay.billing_address = billing() | {"billing_postcode": "AbulazizAlsulami"}
    stay.save(update_fields=["billing_address"])
    page = Client().get(urlsplit(public_url(stay)).path)
    assert page.status_code == 200
    assert "عنوان الفوترة المحفوظ".encode() in page.content
    assert b'name="terms_accepted"' not in page.content
    assert not MorningPaymentAttempt.objects.exists()


def test_morning_backend_rejects_bad_billing_even_if_client_js_is_skipped():
    prop, hostaway = property_and_remote()
    stay = create(prop, hostaway)
    with pytest.raises(HyperPayCheckoutError, match="billing_address_invalid"):
        create_checkout(
            stay.pk, billing_data=billing() | {"billing_postcode": "AbulazizAlsulami"},
            acceptance=consent(), hostaway=hostaway,
        )
    stay.refresh_from_db()
    assert stay.status == "open"
    assert not stay.attempts.exists()


@pytest.mark.parametrize(
    "name,email,valid",
    [
        ("Synthetic Guest", "morning@example.test", True),
        ("A" * 51 + " Guest", "morning@example.test", False),
        ("Guest " + "B" * 51, "morning@example.test", False),
        ("Single", "morning@example.test", False),
        ("Synthetic Guest", "a" * 125 + "@example.test", False),
        ("Synthetic Guest", "not-an-email", False),
    ],
)
def test_admin_identity_is_validated_to_hyperpay_limits(name, email, valid):
    prop, _ = property_and_remote()
    start, _ = window()
    form = MorningStayForm({
        "property": prop.pk,
        "date": start.date(),
        "start_time": "01:00",
        "end_time": "12:00",
        "guest_name": name,
        "guest_phone": "+16502530000",
        "guest_email": email,
        "guests": 1,
        "amount_sar": "500",
        "request_id": __import__("uuid").uuid4(),
    })
    assert form.is_valid() is valid


def test_morning_admin_and_guest_pages_show_visible_per_field_rules():
    stay = create()
    admin = Client()
    admin.force_login(stay.created_by)
    urls = (
        reverse("notifications:morning_create"),
        reverse("notifications:morning_detail", args=[stay.pk]),
        urlsplit(public_url(stay)).path,
    )
    for url in urls:
        client = Client() if url.startswith("/payments/") else admin
        response = client.get(url)
        assert response.status_code == 200
        assert b"morning-billing-validation.css" in response.content
        assert b"morning-billing-validation.js" in response.content
        assert b"id_billing_postcode_rule" in response.content
        assert "الرمز البريدي".encode() in response.content
        assert "5".encode() in response.content


def test_regular_booking_form_is_not_modified():
    from apps.payments.payment_link_views import PaymentLinkBillingForm

    old = PaymentLinkBillingForm(billing() | {
        "billing_postcode": "AbulazizAlsulami",
    })
    assert old.fields["billing_postcode"].max_length == 16
    assert "data-morning-billing" not in old.fields["billing_postcode"].widget.attrs
