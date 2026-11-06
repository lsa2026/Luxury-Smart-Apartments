"""Owner payment-link policy, isolated providers and synthetic guest records."""

from datetime import timedelta
from unittest.mock import patch

import pytest
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from apps.payments.hyperpay.exceptions import HyperPayCheckoutError
from apps.payments.morning_payments import create_checkout, public_url, require_payable
from apps.reservations.morning_billing import MorningBillingForm
from apps.reservations.morning_stays import validate_window
from apps.reservations.morning_views import MorningStayForm
from tests.test_hyperpay import HyperPayStub
from tests.test_morning_stays import (
    approved,
    consent,
    create,
    property_and_remote,
    verify_payment,
    window,
)
from tests.test_morning_stays import isolated as isolated
from tests.test_reservation_payment_links import billing, form_data

pytestmark = pytest.mark.django_db


def test_noon_checkout_is_valid_and_default():
    start, end = window()
    validate_window(start, end.replace(hour=12))
    assert MorningStayForm().fields["end_time"].initial.hour == 12


@pytest.mark.parametrize("offset", [timedelta(minutes=-1), timedelta(hours=1), timedelta(hours=12)])
def test_link_has_no_clock_expiry_but_final_states_close_it(offset):
    stay = create()
    original = public_url(stay)
    with patch("django.utils.timezone.now", return_value=stay.starts_at + offset):
        stay.expires_at = timezone.now() - timedelta(days=1)
        require_payable(stay)
        assert public_url(stay) == original
        for status in ["cancelled", "paid", "review"]:
            stay.status = status
            with pytest.raises(HyperPayCheckoutError):
                require_payable(stay)


def test_late_payment_records_financial_success_without_admission():
    p, remote = property_and_remote()
    stay = create(p, remote)
    gateway = HyperPayStub()
    with patch("django.utils.timezone.now", return_value=stay.ends_at + timedelta(minutes=20)):
        attempt = create_checkout(
            stay.pk, billing_data=billing(), acceptance=consent(), client=gateway, hostaway=remote
        )
        with patch.object(gateway, "get_checkout_payment", return_value=approved(attempt)):
            verify_payment(attempt.pk, client=gateway)
    stay.refresh_from_db()
    assert stay.paid_at and stay.status == "review"


def test_admin_saves_address_and_guest_only_reviews_and_consents():
    stay = create()
    client = Client()
    client.force_login(stay.created_by)
    admin_url = reverse("notifications:morning_detail", args=[stay.pk])
    assert client.post(admin_url, {"action": "save_billing", **billing()}).status_code == 302
    stay.refresh_from_db()
    assert stay.billing_address == billing()
    url = public_url(stay).replace("https://booking.example", "")
    from urllib.parse import urlsplit

    url = urlsplit(url).path
    page = Client().get(url)
    assert page.status_code == 200
    assert b'name="billing_street1"' not in page.content
    assert b"King Road 10" in page.content
    with patch("apps.payments.morning_views.create_checkout") as checkout:
        Client().post(url, {})
    checkout.assert_not_called()
    with patch(
        "apps.payments.morning_views.create_checkout",
        side_effect=HyperPayCheckoutError("synthetic"),
    ) as checkout:
        Client().post(url, form_data() | {"billing_street1": "tampered"})
    assert checkout.call_args.kwargs["billing_data"] == billing()


def test_saved_billing_is_validated_and_used_in_provider_payload():
    p, remote = property_and_remote()
    stay = create(p, remote)
    stay.billing_address = billing()
    stay.save()
    gateway = HyperPayStub()
    create_checkout(
        stay.pk,
        billing_data=billing() | {"billing_city": "tampered"},
        acceptance=consent(),
        client=gateway,
        hostaway=remote,
    )
    assert gateway.payload["billing.city"] == "Riyadh"
    assert MorningBillingForm(billing()).is_valid()
    assert not MorningBillingForm(billing() | {"billing_country": "ZZ"}).is_valid()


def test_admin_can_supply_address_during_creation():
    p, _ = property_and_remote()
    import uuid

    start, _ = window()
    form = MorningStayForm(
        {
            "request_id": uuid.uuid4(),
            "property": p.pk,
            "date": start.date(),
            "start_time": "01:00",
            "end_time": "12:00",
            "guest_name": "Synthetic Guest",
            "guest_phone": "+16502530000",
            "guest_email": "synthetic@example.test",
            "guests": 1,
            "amount_sar": "500.00",
            **billing(),
        }
    )
    assert form.is_valid(), form.errors
    assert form.cleaned_data["billing_address"] == billing()


def test_admin_address_search_is_not_public():
    url = reverse("notifications:morning_address", args=["suggestions"])
    assert Client().post(url, {}, content_type="application/json").status_code in {302, 403}


def test_unpaid_stay_can_extend_to_noon_without_replacing_link():
    from apps.reservations.morning07_changes import change_paid_stay

    p, remote = property_and_remote()
    stay = create(p, remote)
    original = public_url(stay)
    values = {
        key: getattr(stay, key)
        for key in (
            "property",
            "starts_at",
            "ends_at",
            "guest_name",
            "guest_phone",
            "guest_email",
            "guest_language",
            "guests",
        )
    }
    from apps.reservations.morning_stays import property_zone

    values["ends_at"] = stay.ends_at.astimezone(property_zone(p)).replace(hour=12)
    change_paid_stay(
        stay.pk, values, expected_updated_at=stay.updated_at.isoformat(), client=remote
    )
    stay.refresh_from_db()
    assert stay.ends_at.astimezone(property_zone(p)).hour == 12
    assert public_url(stay) == original and stay.status == "open"
