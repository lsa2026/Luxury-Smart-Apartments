"""Synthetic customer data, real forms/views/service, no live gateway or guest sends."""

import json
import subprocess
from unittest.mock import Mock, patch
from urllib.parse import urlsplit

import pytest
from django.core.exceptions import ValidationError
from django.test import Client
from django.utils import translation

from apps.payments.hyperpay.exceptions import HyperPayCheckoutError
from apps.payments.models import MorningPaymentAttempt
from apps.payments.morning_payments import create_checkout, public_url
from apps.payments.morning_validation import RULES, MorningPaymentForm, validate_value
from apps.reservations.morning_billing import MorningBillingForm
from tests.test_hyperpay import HyperPayStub
from tests.test_morning_stays import consent, create, property_and_remote
from tests.test_morning_stays import isolated as isolated  # noqa: F401
from tests.test_reservation_payment_links import billing, form_data

pytestmark = pytest.mark.django_db

CASES = [
    ("guest_first_name", "سعيد", True),
    ("guest_first_name", "O'Neil", True),
    ("guest_first_name", "😀" * 50, True),
    ("guest_first_name", "A" * 51, False),
    ("guest_last_name", "A" * 50, True),
    ("guest_last_name", "A" * 51, False),
    ("guest_email", "a@b.co", True),
    ("guest_email", "x" * 116 + "@example.com", True),
    ("guest_email", "x" * 117 + "@example.com", False),
    ("guest_email", "not-an-email", False),
    ("guest_email", "a b@example.com", False),
    ("guest_email", "a@b.c", False),
    ("billing_street1", "شارع الملك ١٠", True),
    ("billing_street1", "X" * 100, True),
    ("billing_street1", "X" * 101, False),
    ("billing_street1", "12345", False),
    ("billing_city", "الرياض", True),
    ("billing_city", "X" * 80, True),
    ("billing_city", "X" * 81, False),
    ("billing_state", "Riyadh.01", True),
    ("billing_state", "X" * 50, True),
    ("billing_state", "X" * 51, False),
    ("billing_state", "Al Riyadh", False),
    ("billing_state", "الرياض", False),
    ("billing_state", "Riyadh-01", False),
    ("billing_postcode", "1234567890ABCDEF", True),
    ("billing_postcode", "1234567890ABCDEFG", False),
    ("billing_postcode", "SW1A 1AA", False),
    ("billing_postcode", "10001-1234", False),
    ("billing_postcode", "١٢٣٤٥", False),
    ("billing_country", "SA", True),
    ("billing_country", "sa", False),
    ("billing_city", "\x00", False),
    *[(name, " \t ", False) for name in RULES],
]


@pytest.mark.parametrize("name,value,valid", CASES)
def test_documented_boundaries(name, value, valid):
    if valid:
        assert validate_value(name, value) == value
    else:
        with pytest.raises(ValidationError):
            validate_value(name, value)


def test_browser_and_server_use_the_same_contract():
    # Execute the actual browser validator against the complete boundary matrix.
    vectors = [
        {
            "value": value,
            "required": True,
            "dataset": {"morningPattern": RULES[name][0], "morningError": "invalid"},
        }
        for name, value, valid in CASES
    ]
    script = """
const {fieldError} = require('./static/js/morning-validation.js');
let input = '';
process.stdin.on('data', chunk => input += chunk);
process.stdin.on('end', () => {
    console.log(JSON.stringify(JSON.parse(input).map(x => !fieldError(x))));
});
"""
    result = subprocess.run(
        ["node", "-e", script],
        input=json.dumps(vectors),
        capture_output=True,
        text=True,
        check=True,
    )
    assert json.loads(result.stdout) == [valid for _, _, valid in CASES]


@pytest.mark.parametrize("lang,phrase", [("ar", "الرمز البريدي"), ("en", "Postal code")])
def test_invalid_post_preserves_input_and_stops_checkout(lang, phrase):
    stay = create()
    stay.guest_language = lang
    stay.save()
    url = urlsplit(public_url(stay)).path
    with patch("apps.payments.morning_views.create_checkout") as checkout:
        response = Client().post(url, form_data() | {"billing_postcode": "bad-post-code"})
    checkout.assert_not_called()
    assert response.status_code == 200
    assert response.context["form"].is_bound
    assert "billing_postcode" in response.context["form"].errors
    assert phrase in response.content.decode()
    assert 'value="bad-post-code"' in response.content.decode()
    assert f'<html lang="{lang}"' in response.content.decode()
    assert not MorningPaymentAttempt.objects.exists()


def test_service_rejects_bypass_before_remote_io_or_claim():
    stay = create()
    gateway, remote = Mock(), Mock()
    with pytest.raises(HyperPayCheckoutError, match="morning_customer_data_invalid"):
        create_checkout(
            stay.pk,
            billing_data=billing() | {"billing_state": "الرياض"},
            acceptance=consent(),
            client=gateway,
            hostaway=remote,
        )
    gateway.create_checkout.assert_not_called()
    remote.get_listing_calendar.assert_not_called()
    assert not MorningPaymentAttempt.objects.exists()
    stay.refresh_from_db()
    assert stay.status == "open"


def test_production_payload_preserves_valid_data_and_has_no_card_fields(settings):
    settings.HYPERPAY_ENVIRONMENT = "production"
    p, remote = property_and_remote()
    stay = create(p, remote)
    gateway = HyperPayStub()
    customer = {
        "guest_first_name": "سعيد",
        "guest_last_name": "O'Neil",
        "guest_email": "payer@example.test",
    }
    create_checkout(
        stay.pk,
        billing_data=billing() | {"billing_state": "Riyadh.01"},
        customer_data=customer,
        acceptance=consent(),
        client=gateway,
        hostaway=remote,
    )
    assert gateway.checkout_calls == 1
    assert gateway.payload["customer.givenName"] == "سعيد"
    assert gateway.payload["customer.surname"] == "O'Neil"
    assert gateway.payload["billing.state"] == "Riyadh.01"
    assert gateway.payload["currency"] == "SAR"
    assert gateway.payload["paymentType"] == "DB"
    assert "testMode" not in gateway.payload
    assert not any(key.startswith("card.") for key in gateway.payload)
    stay.refresh_from_db()
    assert stay.guest_name == "Synthetic Guest"


def test_invalid_saved_address_is_explained_and_cannot_be_replaced_by_post():
    stay = create()
    stay.billing_address = billing() | {"billing_state": "Invalid Region"}
    stay.save()
    url = urlsplit(public_url(stay)).path
    page = Client().get(url)
    assert not page.context["payable"]
    assert "billing_state" in page.context["address_errors"].errors
    assert b"data-morning-validation" not in page.content
    with patch("apps.payments.morning_payments.verify_window") as remote:
        with pytest.raises(HyperPayCheckoutError):
            create_checkout(stay.pk, billing_data=billing(), acceptance=consent())
    remote.assert_not_called()


def test_unknown_country_and_stale_terms_are_rejected():
    assert not MorningBillingForm(billing() | {"billing_country": "ZZ"}).is_valid()
    with translation.override("en"):
        form = MorningPaymentForm(
            form_data()
            | {
                "guest_first_name": "Synthetic",
                "guest_last_name": "Guest",
                "guest_email": "synthetic@example.test",
                "documents_digest": "old",
            }
        )
        assert not form.is_valid()
        assert "documents_digest" in form.errors


def test_guest_page_contains_no_merchant_card_inputs_and_has_persistent_help():
    response = Client().get(urlsplit(public_url(create())).path)
    html = response.content.decode()
    for name in RULES:
        assert f'id="id_{name}_help"' in html
        assert f'id="id_{name}_error"' in html
    for name in ("card.number", "card.cvv", "card.expiryMonth", "card.holder"):
        assert f'name="{name}"' not in html
    assert "data-morning-validation" in html
