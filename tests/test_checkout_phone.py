from unittest.mock import patch

import pytest
from django.conf import settings
from django.core.cache import cache
from django.test import Client
from django.utils.translation import override

from apps.core.phone_numbers import InvalidPhoneNumber, normalize_phone_number
from apps.reservations.booking_forms import GuestDetailsForm
from apps.reservations.models import BookingIntent, Reservation
from apps.reservations.views import GuestDetailsView
from tests.test_booking_models_services import make_availability
from tests.test_booking_views_admin import RevalidationService, form_data, owned_client_quote

pytestmark = pytest.mark.django_db


@pytest.mark.parametrize(
    ("raw", "country", "expected"),
    [
        ("0501234567", "SA", "+966501234567"),
        ("501234567", "SA", "+966501234567"),
        ("966501234567", "SA", "+966501234567"),
        ("00966 50 123 4567", "SA", "+966501234567"),
        ("٠٥٠١٢٣٤٥٦٧", "SA", "+966501234567"),
        ("۰۵۰۱۲۳۴۵۶۷", "SA", "+966501234567"),
        ("(050) 123-4567", "SA", "+966501234567"),
        ("+1 212 555 1234", "SA", "+12125551234"),
        ("2125551234", "US", "+12125551234"),
        ("0612345678", "MA", "+212612345678"),
        ("06 12 34 56 78", "FR", "+33612345678"),
        ("0033612345678", "SA", "+33612345678"),
        # Italy's leading zero is significant and must survive conversion.
        ("02 36618 300", "IT", "+390236618300"),
    ],
)
def test_checkout_normalizes_national_and_international_numbers(raw, country, expected):
    form = GuestDetailsForm(
        {
            **form_data(),
            "guest_phone": raw,
            "guest_phone_country": country,
            "billing_country": "MA",  # Must not determine the phone's country.
        }
    )
    assert form.is_valid(), form.errors
    assert form.cleaned_data["guest_phone"] == expected


@pytest.mark.parametrize("raw", ["12345", "+99912345678", "050ABC1234567", "++966501234567"])
def test_checkout_invalid_number_is_a_field_error_and_preserves_other_values(raw):
    form = GuestDetailsForm({**form_data(), "guest_phone": raw})
    assert not form.is_valid()
    assert "guest_phone" in form.errors
    assert form["guest_email"].value() == form_data()["guest_email"]


def test_invalid_country_is_not_silently_defaulted():
    form = GuestDetailsForm({**form_data(), "guest_phone_country": "XX"})
    assert not form.is_valid()
    assert "guest_phone_country" in form.errors


def test_old_strict_callers_are_not_changed():
    with pytest.raises(InvalidPhoneNumber):
        normalize_phone_number("0501234567")


def test_check_is_session_owned_csrf_protected_and_does_not_create_bookings():
    cache.clear()
    client, quote, reference = owned_client_quote()
    url = f"/reservations/quotes/{reference}/phone-check/"
    data = {"phone": "0501234567", "country": "SA"}
    response = client.post(url, data)
    assert response.status_code == 200
    assert response.json() == {"phone": "+966501234567", "country": "SA"}
    assert response["Cache-Control"] == "private, no-store"
    assert not BookingIntent.objects.exists()
    assert not Reservation.objects.exists()
    assert Client().post(url, data).status_code == 404
    assert Client(enforce_csrf_checks=True).post(url, data).status_code == 403
    assert client.get(url).status_code == 405
    invalid = client.post(url, {"phone": "12345", "country": "SA"})
    assert invalid.status_code == 400
    assert invalid.json() == {"detail": "invalid_phone"}
    international = client.post(url, {"phone": "+12125551234", "country": "SA"})
    assert international.json()["country"] == "US"


def test_check_fails_safely_for_expired_quote_and_rate_limits():
    cache.clear()
    client, quote, reference = owned_client_quote()
    url = f"/reservations/quotes/{reference}/phone-check/"
    data = {"phone": "0501234567", "country": "SA"}
    with patch("apps.reservations.views.is_rate_limited", return_value=True):
        assert client.post(url, data).status_code == 429
    quote.status = quote.Status.EXPIRED
    quote.save(update_fields=["status"])
    assert client.post(url, data).status_code == 409


@pytest.mark.parametrize(
    ("language", "label"),
    [
        ("ar", "بلد رقم الجوال"),
        ("en", "Phone number country"),
        ("fr", "Pays du numéro de téléphone"),
    ],
)
def test_quote_has_localized_accessible_country_picker(language, label):
    cache.clear()
    client, _, reference = owned_client_quote()
    client.cookies[settings.LANGUAGE_COOKIE_NAME] = language
    with override(language):
        response = client.get(f"/reservations/quotes/{reference}/")
    html = response.content.decode()
    assert response.status_code == 200
    assert label in html
    assert '<option value="SA" selected>' in html
    assert 'name="guest_phone_country"' in html
    assert 'aria-describedby="guest-phone-help guest-phone-validation"' in html
    assert "checkout-phone.js" in html


def test_real_guest_details_path_receives_canonical_phone_without_live_payment(monkeypatch):
    cache.clear()
    client, quote, reference = owned_client_quote()
    RevalidationService.result = make_availability(quote.property)
    monkeypatch.setattr(GuestDetailsView, "service_class", RevalidationService)
    response = client.post(
        f"/reservations/quotes/{reference}/guest-details/",
        {**form_data(), "guest_phone": "0501234567", "guest_phone_country": "SA"},
    )
    assert response.status_code == 302
    assert BookingIntent.objects.get().guest_phone == "+966501234567"
    assert not Reservation.objects.exists()
