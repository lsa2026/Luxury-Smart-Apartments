"""Synthetic booking inputs and private all-channel checks, without live IO."""

# ruff: noqa: F811 -- imported pytest fixtures are injected by name.

import os
from datetime import timedelta
from pathlib import Path

import pytest
from django.contrib.auth import get_user_model
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from apps.core.guest_documents import documents_digest
from apps.core.phone_numbers import InvalidPhoneNumber, normalize_booking_phone
from apps.reservations import guest_check_views
from apps.reservations.booking_forms import GuestDetailsForm
from apps.reservations.day_use_views import DayUseStayForm
from apps.reservations.forms import ReservationAccessForm
from apps.reservations.models import BookingIntent, Reservation
from apps.reservations.morning_views import MorningStayForm
from apps.reservations.operations_forms import ManualBookingFinalizeForm
from apps.reservations.services.guest_classification import contact_keys, guest_classifications
from tests.test_contact_layout import _standalone
from tests.test_guest_classification import factory as factory
from tests.test_guest_classification import isolated as isolated
from tests.test_guest_classification import owner_client as owner_client
from tests.test_guest_classification import publish as publish
from tests.test_guest_classification import row
from tests.test_manual_guest_language import guest_fields, make_draft

pytestmark = pytest.mark.django_db

INPUTS = [
    ("0501234567", "", "+966501234567"),
    ("501234567", "SA", "+966501234567"),
    ("٠٥٠١٢٣٤٥٦٧", "SA", "+966501234567"),
    ("00966501234567", "SA", "+966501234567"),
    ("+44 (20) 7946-0958", "SA", "+442079460958"),
    ("0044 20 7946 0958", "SA", "+442079460958"),
    ("020 7946 0958", "GB", "+442079460958"),
    ("16465550123", "SA", "+16465550123"),
    ("+6462817246", "SA", "+6462817246"),
]


@pytest.mark.parametrize("phone,country,expected", INPUTS)
def test_all_booking_forms_share_correction_and_sa_default(phone, country, expected):
    draft = make_draft()
    data = guest_fields(draft) | {"guest_phone": phone, "guest_phone_country": country}
    manual = ManualBookingFinalizeForm(data, draft=draft, require_address=True)
    assert manual.is_valid(), manual.errors
    assert manual.cleaned_data["guest_phone"] == expected
    assert manual.cleaned_data["billing_country"] == "SA"
    public = GuestDetailsForm(
        data
        | {
            "terms_accepted": "on",
            "house_rules_accepted": "on",
            "documents_digest": documents_digest(),
            "idempotency_key": "x" * 32,
        }
    )
    assert public.is_valid(), public.errors
    assert public.cleaned_data["guest_phone"] == expected
    for cls in (MorningStayForm, DayUseStayForm):
        form = cls(data)
        form.is_valid()
        assert "guest_phone" not in form.errors
        assert form.cleaned_data["guest_phone"] == expected
    access = ReservationAccessForm(
        {"last_name": "Guest", "phone": phone, "guest_phone_country": country}
    )
    assert access.is_valid(), access.errors
    assert access.cleaned_data["phone"] == expected
    assert not Reservation.objects.exists() and not BookingIntent.objects.exists()


@pytest.mark.parametrize(
    "phone,country", [("123", "SA"), ("0501234567", "ZZ"), ("hello", "SA"), ("6462817246", "SA")]
)
def test_invalid_input_cannot_bypass_final_server_validation(phone, country):
    with pytest.raises(InvalidPhoneNumber):
        normalize_booking_phone(phone, default_region=country)
    draft = make_draft()
    form = ManualBookingFinalizeForm(
        guest_fields(draft) | {"guest_phone": phone, "guest_phone_country": country},
        draft=draft,
        require_address=True,
    )
    assert not form.is_valid()


@pytest.mark.parametrize("phone,country,expected", INPUTS)
def test_format_endpoint_is_local_post_only_and_does_not_establish_access(phone, country, expected):
    client = Client()
    url = reverse("reservations:booking_phone_format")
    assert client.get(url).status_code == 405
    response = client.post(url, {"phone": phone, "country": country})
    assert response.status_code == 200
    assert response.json()["phone"] == expected
    assert "no-store" in response["Cache-Control"]
    assert not client.session.get("reservation_access")
    assert not Reservation.objects.exists() and not BookingIntent.objects.exists()
    assert Client(enforce_csrf_checks=True).post(url, {"phone": phone}).status_code == 403


def test_format_endpoint_limits_abuse_and_rejects_invalid_country():
    url = reverse("reservations:booking_phone_format")
    client = Client()
    assert client.post(url, {"phone": "0501234567", "country": "ZZ"}).status_code == 400
    assert client.post(url, {"phone": "5" * 33}).status_code == 400
    for _ in range(58):
        assert client.post(url, {"phone": "0501234567"}).status_code == 200
    assert client.post(url, {"phone": "0501234567"}).status_code == 429


def test_identical_national_digits_in_distinct_countries_do_not_match(factory, publish):
    us, gb = "+12025550100", "+442025550100"
    assert contact_keys("", us)[1] != contact_keys("", gb)[1]
    current = factory(phone=gb)
    publish(
        [
            [
                row(777, "previous@example.invalid", us),
                row(current.hostaway_reservation_id, phone=gb),
            ],
            [],
        ]
    )
    assert guest_classifications()[current.pk].code == "new"


def test_ambiguous_national_number_cannot_be_guessed_as_foreign_country():
    assert contact_keys("", "6462817246")[1] == ""
    assert contact_keys("", "+6462817246")[1]


def check_url():
    return reverse("notifications:guest_classification_check")


@pytest.mark.parametrize(
    "input_data",
    [{"phone": "0501234567"}, {"phone": "00966501234567"}, {"email": "PRIOR@example.invalid"}],
)
def test_new_booking_check_matches_all_channel_hostaway_contacts_only(
    factory, publish, owner_client, monkeypatch, input_data
):
    current = factory()
    publish(
        [
            [
                row(current.hostaway_reservation_id),
                row(777, "prior@example.invalid", "+966501234567", channel=2002),
            ],
            [],
        ]
    )
    monkeypatch.setattr(guest_check_views, "ensure_refresh", lambda **kwargs: None)
    before = (Reservation.objects.count(), BookingIntent.objects.count())
    result = owner_client.post(check_url(), input_data)
    assert result.status_code == 200
    assert result.json()["classification"]["code"] == "returning"
    assert "private" in result["Cache-Control"] and "no-store" in result["Cache-Control"]
    assert before == (Reservation.objects.count(), BookingIntent.objects.count())
    assert Client().post(check_url(), input_data).status_code in {302, 403}


def test_booking_check_uses_provider_identity_and_excludes_itself(
    factory, publish, owner_client, monkeypatch
):
    current = factory(email="local@example.invalid", phone="+966501234567")
    publish(
        [
            [
                row(current.hostaway_reservation_id, "provider@example.invalid", "+442079460958"),
                row(777, "local@example.invalid", "+966501234567"),
            ],
            [],
        ]
    )
    monkeypatch.setattr(guest_check_views, "ensure_refresh", lambda **kwargs: None)
    result = owner_client.post(
        check_url(),
        {"booking_id": str(current.pk), "email": "local@example.invalid", "phone": "+966501234567"},
    )
    assert result.json()["classification"]["code"] == "new"
    assert owner_client.post(check_url(), {"booking_id": "bad"}).status_code == 400


def test_refresh_starts_source_read_only_and_never_claims_new_while_pending(owner_client, isolated):
    result = owner_client.post(check_url(), {"phone": "0501234567", "refresh": "1"})
    assert result.status_code == 200 and isolated
    assert result.json()["status"] == "queued"
    assert result.json()["classification"]["code"] == "unknown"
    assert not Reservation.objects.exists() and not BookingIntent.objects.exists()
    assert owner_client.post(check_url(), {}).status_code == 400


def test_private_check_requires_csrf(owner_client):
    guarded = Client(enforce_csrf_checks=True)
    guarded.force_login(get_user_model().objects.get(pk=owner_client.session["_auth_user_id"]))
    assert guarded.post(check_url(), {"phone": "0501234567"}).status_code == 403


def test_render_unified_phone_layout(tmp_path):
    draft = make_draft()
    data = guest_fields(draft)
    for name in ("guest_first_name", "guest_last_name", "guest_email", "guest_phone"):
        setattr(draft, name, data[name])
    draft.billing_address = {
        key: value for key, value in data.items() if key.startswith("billing_")
    }
    draft.quote.expires_at = timezone.now() + timedelta(hours=1)
    draft.quote.save(update_fields=["expires_at"])
    draft.save()
    client = Client()
    client.force_login(draft.created_by)
    response = client.get(reverse("notifications:manual_booking_detail", args=[draft.pk]))
    assert response.status_code == 200
    html = response.content.decode()
    assert html.count('name="guest_phone_country"') == 1
    assert "data-booking-phone" in html and "data-admin-guest-check" in html
    assert 'name="billing_country"' in html
    scripts = "".join(
        f'<script src="{Path("static/js", filename).resolve().as_uri()}"></script>'
        for filename in ("morning-validation.js", "checkout-phone.js", "admin-guest-check.js")
    )
    output = Path(os.environ.get("LSA_CONTACT_VISUAL_DIR", tmp_path))
    output.mkdir(parents=True, exist_ok=True)
    (output / "unified-phone.html").write_text(_standalone(html) + scripts, encoding="utf-8")
