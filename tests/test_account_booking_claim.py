"""Attaching a guest booking to an account, and refusing to when unproven."""

from decimal import Decimal

import pytest
from django.contrib.auth import get_user_model
from django.test import Client
from django.utils import timezone

from apps.properties.models import Property
from apps.reservations.models import BookingIntent, BookingQuote, Reservation
from apps.reservations.security import grant_reservation_access

pytestmark = pytest.mark.django_db

User = get_user_model()

REGISTRATION = {
    "first_name": "Guest",
    "last_name": "Example",
    "email": "guest@example.invalid",
    "password1": "Correct-Horse-Battery-2026",
    "password2": "Correct-Horse-Battery-2026",
    "accept_terms": "on",
}


def make_reservation(reference: str = "LSA-CLAIM-1") -> Reservation:
    now = timezone.now()
    check_in = now.date() + timezone.timedelta(days=7)
    check_out = check_in + timezone.timedelta(days=2)
    property_obj = Property.objects.create(
        hostaway_listing_id=abs(hash(reference)) % 1_000_000,
        slug=f"claim-{reference.lower()}",
        hostaway_name="Source",
        name_ar="وحدة",
        name_en="Unit",
        city="Riyadh",
        country_code="SA",
        currency_code="SAR",
        is_visible=True,
    )
    quote = BookingQuote.objects.create(
        property=property_obj,
        hostaway_listing_id=property_obj.hostaway_listing_id,
        check_in=check_in,
        check_out=check_out,
        nights=2,
        guests=2,
        currency="SAR",
        total_price=Decimal("500.00"),
        signature=f"signature-{reference}",
        session_key_hash=f"hash-{reference}",
        expires_at=now + timezone.timedelta(hours=1),
        calculated_at=now,
    )
    intent = BookingIntent.objects.create(
        quote=quote,
        property=property_obj,
        check_in=check_in,
        check_out=check_out,
        nights=2,
        guests=2,
        currency="SAR",
        total_price=Decimal("500.00"),
        guest_first_name="Guest",
        guest_last_name="Example",
        guest_email="guest@example.invalid",
        guest_phone="+966500000000",
        guest_country_code="SA",
        idempotency_key=f"idempotency-{reference}",
        session_key_hash=f"hash-{reference}",
        terms_accepted_at=now,
        privacy_accepted_at=now,
        expires_at=now + timezone.timedelta(hours=1),
    )
    return Reservation.objects.create(
        property=property_obj,
        booking_intent=intent,
        public_reference=reference,
        check_in=check_in,
        check_out=check_out,
        nights=2,
        guests=2,
        currency="SAR",
        total_price=Decimal("500.00"),
    )


def client_holding(reservation: Reservation) -> Client:
    """A client whose session carries the grant issued after payment."""
    client = Client()
    request = client.request().wsgi_request
    grant_reservation_access(request, reservation.public_reference)
    request.session.save()
    client.cookies["sessionid"] = request.session.session_key
    return client


def test_registering_from_a_proven_session_attaches_the_booking() -> None:
    reservation = make_reservation()
    client = client_holding(reservation)

    response = client.post(
        "/register/",
        {**REGISTRATION, "claim": reservation.public_reference},
        follow=True,
    )

    assert response.status_code == 200
    reservation.booking_intent.refresh_from_db()
    assert reservation.booking_intent.customer is not None
    assert reservation.booking_intent.customer.email == "guest@example.invalid"


def test_the_attached_booking_then_appears_on_the_dashboard() -> None:
    reservation = make_reservation()
    client = client_holding(reservation)
    client.post("/register/", {**REGISTRATION, "claim": reservation.public_reference})

    content = client.get("/my-bookings/").content.decode()

    assert reservation.public_reference in content


def test_a_matching_email_alone_never_attaches_a_booking() -> None:
    """The security case: registering with a guest's address proves nothing."""
    reservation = make_reservation()
    # A fresh client: same email as the booking, but no session grant.
    client = Client()

    client.post(
        "/register/",
        {**REGISTRATION, "claim": reservation.public_reference},
        follow=True,
    )

    reservation.booking_intent.refresh_from_db()
    assert reservation.booking_intent.customer is None


def test_a_forged_reference_is_ignored_even_from_a_proven_session() -> None:
    reservation = make_reservation()
    other = make_reservation("LSA-CLAIM-2")
    # The session proves only the first booking.
    client = client_holding(reservation)

    client.post("/register/", {**REGISTRATION, "claim": other.public_reference}, follow=True)

    other.booking_intent.refresh_from_db()
    assert other.booking_intent.customer is None


def test_signing_in_attaches_the_booking_for_an_existing_account() -> None:
    reservation = make_reservation()
    User.objects.create_user(
        username="guest@example.invalid",
        email="guest@example.invalid",
        password="Correct-Horse-Battery-2026",
    )
    client = client_holding(reservation)

    client.post(
        "/login/",
        {
            "username": "guest@example.invalid",
            "password": "Correct-Horse-Battery-2026",
            "claim": reservation.public_reference,
        },
        follow=True,
    )

    reservation.booking_intent.refresh_from_db()
    assert reservation.booking_intent.customer is not None


def test_a_booking_already_owned_is_never_moved_between_accounts() -> None:
    reservation = make_reservation()
    owner = User.objects.create_user(
        username="owner@example.invalid",
        email="owner@example.invalid",
        password="Correct-Horse-Battery-2026",
    )
    reservation.booking_intent.customer = owner
    reservation.booking_intent.save(update_fields=["customer"])
    client = client_holding(reservation)

    client.post(
        "/register/",
        {**REGISTRATION, "claim": reservation.public_reference},
        follow=True,
    )

    reservation.booking_intent.refresh_from_db()
    assert reservation.booking_intent.customer == owner
