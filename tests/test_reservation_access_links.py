"""The easy booking-recovery form must never disclose a stay directly."""

import pytest
from django.core import mail
from django.test import Client, override_settings

from apps.notifications.models import EmailDelivery
from apps.notifications.services.email import recipient_hmac, send_queued_email
from apps.reservations.access_tokens import make_access_link_token
from tests.test_account_booking_claim import make_reservation


pytestmark = pytest.mark.django_db


def test_last_name_and_phone_queue_a_secure_link_without_opening_the_booking() -> None:
    reservation = make_reservation("LSA-RECOVERY-1")
    client = Client()

    response = client.post(
        "/reservations/manage/",
        {"last_name": "Example", "phone": "+966 500 000 000"},
    )

    assert response.status_code == 302
    assert response.url == "/reservations/manage/"
    assert client.get(f"/reservations/manage/{reservation.public_reference}/").status_code == 404
    delivery = EmailDelivery.objects.get(message_type="reservation_access_link")
    assert delivery.recipient_reference == reservation.public_reference


def test_recovery_reply_does_not_reveal_that_no_booking_matched() -> None:
    reservation = make_reservation("LSA-RECOVERY-2")
    client = Client()

    response = client.post(
        "/reservations/manage/",
        {"last_name": "Nobody", "phone": "+966500000000"},
        follow=True,
    )

    assert response.status_code == 200
    assert "secure link" in response.content.decode().lower()
    assert not EmailDelivery.objects.filter(message_type="reservation_access_link").exists()
    assert client.get(f"/reservations/manage/{reservation.public_reference}/").status_code == 404


def test_secure_link_opens_a_booking_once() -> None:
    reservation = make_reservation("LSA-RECOVERY-3")
    token = make_access_link_token(reservation.public_reference, "guest@example.invalid")
    client = Client()

    first = client.get(f"/reservations/manage/access/{token}/")

    assert first.status_code == 302
    assert first.url == f"/reservations/manage/{reservation.public_reference}/"
    assert client.get(first.url).status_code == 200
    second = client.get(f"/reservations/manage/access/{token}/")
    assert second.status_code == 302
    assert second.url == "/reservations/manage/"


@override_settings(
    EMAIL_DELIVERY_ENABLED=True,
    EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
    DEFAULT_FROM_EMAIL="notifications@example.invalid",
    SUPPORT_EMAIL="care@example.invalid",
    SITE_BASE_URL="https://stays.example.invalid",
)
def test_recovery_email_is_branded_and_contains_a_secure_action() -> None:
    reservation = make_reservation("LSA-RECOVERY-4")
    delivery = EmailDelivery.objects.create(
        message_type="reservation_access_link",
        recipient_hash=recipient_hmac("guest@example.invalid"),
        recipient_masked="g***@example.invalid",
        recipient_source="reservation",
        recipient_reference=reservation.public_reference,
        language="ar",
        subject="رابط آمن لإدارة حجزك",
        template_name="account",
        status=EmailDelivery.Status.QUEUED,
        provider="django",
        idempotency_key="test-recovery-email-unique-key",
        queued_at=reservation.created_at,
    )

    result = send_queued_email(delivery.pk)

    assert result.sent is True
    html = mail.outbox[-1].alternatives[0].content
    assert "Luxury Smart Apartments" in html
    assert "فتح حجزي بأمان" in html
    assert "/reservations/manage/access/" in html
