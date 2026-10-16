"""Payment wording and icon regression; fictional accounts, no provider writes."""

from unittest.mock import patch

import pytest
from django.conf import settings
from django.contrib.auth import get_user_model
from django.test import Client
from django.utils import translation

from apps.core.templatetags.presentation import (
    localized_payment_status,
    normalized_payment_status,
)
from apps.loyalty.models import Ledger
from tests.test_account_booking_claim import make_reservation


@pytest.mark.parametrize("language", ["ar", "en", "fr"])
@pytest.mark.parametrize(
    ("status", "message"),
    [
        ("paid", "Payment verified"),
        ("succeeded", "Payment verified"),
        ("sandbox_paid", "Test payment completed"),
        ("created", "Payment pending"),
        ("pending", "Payment pending"),
        ("failed", "Payment was not successful"),
        ("cancelled", "Payment was cancelled"),
        ("refunded", "Refunded"),
        ("partially_refunded", "Partially refunded"),
        ("review", "Payment needs review"),
        ("unknown", "Payment needs review"),
        ("unrecognized_provider_state", "Under review"),
    ],
)
def test_known_payment_meaning_survives_provider_case_and_whitespace(language, status, message):
    incoming = f" \t{status.swapcase()}\n"
    with translation.override(language):
        assert localized_payment_status(incoming) == translation.gettext(message)
    assert normalized_payment_status(incoming) == status


@pytest.mark.parametrize("language", ["ar", "en", "fr"])
@pytest.mark.parametrize("status", [None, "", " \t\n"])
def test_missing_payment_state_is_never_claimed_paid(language, status):
    with translation.override(language):
        assert localized_payment_status(status) == translation.gettext("Not paid")


@pytest.mark.django_db
@pytest.mark.parametrize("language", ["ar", "en", "fr"])
@pytest.mark.parametrize("status", ["Paid", "PAID", " \tpAiD\n", " Succeeded "])
def test_guest_booking_card_shows_verified_payment_without_modifying_the_record(language, status):
    user = get_user_model().objects.create_user(
        username="payment-display", email="payment-display@example.invalid"
    )
    reservation = make_reservation("PAYMENT-DISPLAY")
    reservation.payment_status = status
    reservation.normalized_status = "confirmed"
    reservation.hostaway_reservation_id = 994455
    reservation.save(
        update_fields=["payment_status", "normalized_status", "hostaway_reservation_id"]
    )
    reservation.booking_intent.customer = user
    reservation.booking_intent.save(update_fields=["customer"])
    original = {
        name: getattr(reservation, name)
        for name in (
            "payment_status",
            "total_price",
            "normalized_status",
            "hostaway_reservation_id",
        )
    }
    client = Client()
    client.force_login(user)
    client.cookies[settings.LANGUAGE_COOKIE_NAME] = language
    with (
        patch("apps.loyalty.site_adapter.schedule_reconciliation") as rewards,
        patch(
            "apps.integrations.hostaway.client.HostawayClient.account_booking_history_page"
        ) as read,
        translation.override(language),
    ):
        response = client.get("/my-bookings/")
        assert response.status_code == 200
        html = response.content.decode()
        assert translation.gettext("Payment verified") in html
        assert translation.gettext("Under review") not in html
        assert 'class="payment-state payment-state--paid"' in html
        assert '<span aria-hidden="true">✓</span>' in html
        rewards.assert_not_called()
        read.assert_not_called()
    reservation.refresh_from_db()
    assert {name: getattr(reservation, name) for name in original} == original
    assert not Ledger.objects.exists()
