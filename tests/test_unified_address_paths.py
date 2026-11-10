"""Address journeys on disposable data, with all external providers mocked."""
# ruff: noqa: F811

from unittest.mock import patch

import pytest
from django.core.exceptions import ValidationError
from django.test import Client
from django.urls import reverse
from django.utils import translation

from apps.core.guest_documents import documents_digest
from apps.payments.models import DayUsePaymentAttempt, PaymentAttempt
from apps.payments.payment_link_views import PaymentLinkBillingForm
from apps.payments.payment_links import create_request_checkout, issue_request
from apps.reservations.booking_forms import BillingAddressForm
from apps.reservations.day_use_billing import DayUseBillingForm, correct_billing
from apps.reservations.day_use_stays import DayUseStayError
from apps.reservations.morning_billing import MorningBillingForm
from tests.test_billing_address_minimum import form as guest_form
from tests.test_booking_views_admin import owned_client_quote
from tests.test_checkout_address_places import isolated_places as isolated_places
from tests.test_day_use08 import rig as rig
from tests.test_day_use08 import stay
from tests.test_manual_booking_address import consents
from tests.test_reservation_payment_links import billing, manual_stay, page_url
from tests.test_reservation_payment_links import isolated_settings as isolated_settings

pytestmark = pytest.mark.django_db


@pytest.mark.parametrize(
    "name,value,valid",
    [
        ("billing_street1", "12345", False),
        ("billing_street1", "شارع ١٠", True),
        ("billing_street1", "😀" * 100, True),
        ("billing_street1", "😀" * 101, False),
        ("billing_city", "الرياض", True),
        ("billing_city", "X" * 81, False),
        ("billing_state", "Riyadh Province", False),
        ("billing_state", "الرياض", False),
        ("billing_state", "Riyadh.01", True),
        ("billing_postcode", "SW1A 1AA", False),
        ("billing_postcode", "10001-1234", False),
        ("billing_postcode", "SW1A1AA", True),
        ("billing_postcode", "١٢٣٤٥", False),
        ("billing_country", "XX", False),
        ("billing_country", "FR", True),
    ],
)
def test_every_billing_entry_uses_the_same_contract(name, value, valid):
    values = billing() | {name: value}
    forms = [
        guest_form(**values),
        PaymentLinkBillingForm(values | consents()),
        BillingAddressForm(values),
        MorningBillingForm(values),
        DayUseBillingForm(values),
    ]
    for form in forms:
        assert form.is_valid() is valid, form.errors
        assert form.fields[name].help_text
        assert form.fields[name].widget.attrs["data-morning-pattern"]
        if not valid:
            assert name in form.errors
            assert form.data[name] == value


@pytest.mark.usefixtures("isolated_places")
@pytest.mark.parametrize(
    "language,phrase", [("ar", "دون مسافات"), ("en", "without spaces"), ("fr", "sans espaces")]
)
def test_public_quote_and_legacy_payment_show_rules_and_short_address_search(
    language, phrase, settings
):
    client, _, reference = owned_client_quote()
    client.cookies[settings.LANGUAGE_COOKIE_NAME] = language
    quote_page = client.get(f"/reservations/quotes/{reference}/")
    request = issue_request(manual_stay(language=language).pk)
    payment_page = Client().get(page_url(request))
    for page in (quote_page, payment_page):
        assert page.status_code == 200
        content = page.content.decode()
        assert phrase in content
        assert "data-morning-validation" in content
        assert "js/morning-validation.js" in content
        assert "data-checkout-address hidden" in content
        assert "id_billing_state_help" in content
        assert "id_billing_postcode_error" in content
    assert not PaymentAttempt.objects.exists()


def test_ordinary_guest_identity_limits_are_preserved():
    form = guest_form(guest_first_name="A" * 100, guest_email="x" * 120 + "@example.invalid")
    assert form.is_valid(), form.errors


def test_direct_payment_link_service_rejects_invalid_address_before_io():
    request = issue_request(manual_stay().pk)
    with patch("apps.payments.payment_links._verify_remote") as provider:
        with pytest.raises(ValidationError):
            create_request_checkout(
                request.pk,
                billing_data=billing() | {"billing_state": "Invalid Region"},
                acceptance={
                    "documents_digest": documents_digest(),
                    "consents": {"terms": True, "house_rules": True},
                },
            )
    provider.assert_not_called()
    assert not PaymentAttempt.objects.exists()


@pytest.mark.usefixtures("isolated_places")
def test_day_use_admin_create_and_correction_include_search_rules_and_keep_invalid_input(
    rig, settings
):
    current = stay(rig)
    settings.DAY_USE08_ENABLED = True
    client = Client()
    client.force_login(rig.actor)
    detail_url = reverse("notifications:day_use_detail", args=[current.pk])
    for url in (reverse("notifications:day_use_create"), detail_url):
        page = client.get(url, HTTP_ACCEPT_LANGUAGE="en")
        assert page.status_code == 200
        assert page.content.count(b"data-checkout-address hidden") == 1
        assert "دون مسافات" in page.content.decode()
        assert b"data-morning-validation" in page.content
        assert b"js/checkout-address.js" in page.content
    invalid = client.post(
        detail_url,
        billing() | {"action": "save_billing", "billing_state": "Invalid Region"},
    )
    assert invalid.status_code == 200
    assert "billing_state" in invalid.context["billing_form"].errors
    assert invalid.context["billing_form"].data["billing_state"] == "Invalid Region"
    current.refresh_from_db()
    assert current.billing_address == billing()


def test_day_use_address_correction_is_local_stale_safe_and_requires_operations_role(rig, settings):
    current = stay(rig)
    settings.DAY_USE08_ENABLED = True
    current.availability_evidence["hostaway_day_use"] = {
        "state": "confirmed",
        "reservation_id": 99808,
    }
    current.save()
    data = billing() | {"billing_city": "الرياض"}
    with patch("apps.reservations.day_use08_communications.queue_stay_event") as messages:
        corrected = correct_billing(
            current.pk, data, expected_updated_at=current.updated_at.isoformat()
        )
        messages.assert_not_called()
    assert corrected.billing_address == data
    with pytest.raises(DayUseStayError):
        correct_billing(current.pk, billing(), expected_updated_at=current.updated_at.isoformat())
    current.refresh_from_db()
    assert current.billing_address == data
    url = reverse("notifications:day_use_detail", args=[current.pk])
    assert Client().post(url, data | {"action": "save_billing"}).status_code in {302, 403}
    assert not DayUsePaymentAttempt.objects.exists()


@pytest.mark.parametrize("state", ["created", "pending", "unknown", "review", "succeeded"])
def test_day_use_correction_cannot_change_an_address_during_or_after_payment(rig, settings, state):
    current = stay(rig)
    settings.DAY_USE08_ENABLED = True
    DayUsePaymentAttempt.objects.create(
        stay=current,
        merchant_transaction_id="address-guard",
        amount=current.amount_sar,
        status=state,
    )
    with pytest.raises(DayUseStayError):
        correct_billing(
            current.pk,
            billing() | {"billing_city": "Jeddah"},
            expected_updated_at=current.updated_at.isoformat(),
        )
    current.refresh_from_db()
    assert current.billing_address == billing()


def test_french_rule_help_and_errors_do_not_fall_back_to_arabic():
    with translation.override("fr"):
        form = BillingAddressForm(billing() | {"billing_state": "Invalid Region"})
        assert not form.is_valid()
        assert "sans espaces" in form.errors["billing_state"][0]
        assert form.fields["billing_postcode"].label == "Code postal"
