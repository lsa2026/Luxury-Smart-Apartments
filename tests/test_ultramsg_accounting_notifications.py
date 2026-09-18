"""The accountant gets one private WhatsApp request per manual reservation."""

from datetime import timedelta
from unittest.mock import patch

import pytest
from django.contrib.auth import get_user_model
from django.test import Client, override_settings
from django.urls import reverse
from django.utils import timezone

from apps.notifications.models import WhatsAppDelivery
from apps.notifications.services.ultramsg import (
    send_manual_payment_link_request,
    send_modification_payment_link_request,
)
from apps.reservations.models import BookingModificationRequest
from tests.test_account_booking_claim import make_reservation

pytestmark = pytest.mark.django_db


@override_settings(
    ULTRAMSG_ENABLED=True,
    ULTRAMSG_API_BASE_URL="https://api.ultramsg.com",
    ULTRAMSG_INSTANCE_ID="instance-test",
    ULTRAMSG_TOKEN="test-token",
    ACCOUNTING_WHATSAPP_NAME="Aseel Hafez",
    ACCOUNTING_WHATSAPP_NUMBER="+966597193102",
)
def test_manual_payment_request_reaches_accounting_once_with_the_hostaway_link():
    reservation = make_reservation("LSA-ULTRAMSG-1")
    reservation.hostaway_reservation_id = 66436724
    reservation.save(update_fields=["hostaway_reservation_id", "updated_at"])

    with patch(
        "apps.notifications.services.ultramsg.UltraMsgClient.send_text",
        return_value={"sent": "true", "id": "provider-message-1"},
    ) as send:
        result = send_manual_payment_link_request(reservation_id=reservation.pk)
        duplicate = send_manual_payment_link_request(reservation_id=reservation.pk)

    assert result.code == "sent"
    assert duplicate.code == "already_sent"
    assert send.call_count == 1
    payload = send.call_args.kwargs
    assert payload["recipient"] == "+966597193102"
    assert "https://dashboard.hostaway.com/reservations/66436724" in payload["body"]
    assert "LSA-ULTRAMSG-1" in payload["body"]
    delivery = WhatsAppDelivery.objects.get(reservation=reservation)
    assert delivery.status == WhatsAppDelivery.Status.SENT
    assert delivery.provider_message_id == "provider-message-1"
    assert "Guest" not in str(delivery.__dict__)


@override_settings(ULTRAMSG_ENABLED=False)
def test_disabled_ultramsg_never_attempts_an_accounting_delivery():
    reservation = make_reservation("LSA-ULTRAMSG-2")

    with patch("apps.notifications.services.ultramsg.UltraMsgClient.send_text") as send:
        result = send_manual_payment_link_request(reservation_id=reservation.pk)

    assert result.code == "disabled"
    assert send.call_count == 0
    assert result.delivery.status == WhatsAppDelivery.Status.DISABLED


@override_settings(
    ULTRAMSG_ENABLED=True,
    ULTRAMSG_API_BASE_URL="https://api.ultramsg.com",
    ULTRAMSG_INSTANCE_ID="instance-test",
    ULTRAMSG_TOKEN="test-token",
    ACCOUNTING_WHATSAPP_NUMBER="+966597193102",
)
def test_message_build_failure_is_recorded_without_turning_booking_flow_into_500():
    reservation = make_reservation("LSA-ULTRAMSG-3")
    reservation.hostaway_reservation_id = 66436725
    reservation.save(update_fields=["hostaway_reservation_id", "updated_at"])

    with (
        patch(
            "apps.notifications.services.ultramsg._manual_payment_message",
            side_effect=RuntimeError("unexpected message data"),
        ),
        patch("apps.notifications.services.ultramsg.UltraMsgClient.send_text") as send,
    ):
        result = send_manual_payment_link_request(reservation_id=reservation.pk)

    assert result.code == "failed"
    assert result.delivery.status == WhatsAppDelivery.Status.FAILED
    assert result.delivery.last_error_code == "message_build_failed"
    assert send.call_count == 0


@override_settings(
    ULTRAMSG_ENABLED=True,
    ULTRAMSG_API_BASE_URL="https://api.ultramsg.com",
    ULTRAMSG_INSTANCE_ID="instance-test",
    ULTRAMSG_TOKEN="test-token",
    ACCOUNTING_WHATSAPP_NUMBER="+966597193102",
)
def test_modification_payment_request_does_not_lock_nullable_booking_joins():
    reservation = make_reservation("LSA-ULTRAMSG-MODIFICATION-1")
    reservation.hostaway_reservation_id = 66436726
    reservation.save(update_fields=["hostaway_reservation_id", "updated_at"])
    modification = BookingModificationRequest.objects.create(
        reservation=reservation,
        request_type=BookingModificationRequest.RequestType.CHANGE_DATES,
        status=BookingModificationRequest.Status.AWAITING_PAYMENT,
        old_check_in=reservation.check_in,
        old_check_out=reservation.check_out,
        new_check_in=reservation.check_in,
        new_check_out=reservation.check_out + timedelta(days=1),
        old_guests=reservation.guests,
        new_guests=reservation.guests,
        old_total=reservation.total_price,
        new_total=reservation.total_price + 50,
        price_difference=50,
        currency=reservation.currency,
        quote_snapshot={},
        idempotency_key="ultramsg-modification-regression-1",
        session_key_hash="ultramsg-modification-session-hash",
        expires_at=timezone.now() + timedelta(days=1),
    )

    with patch(
        "apps.notifications.services.ultramsg.UltraMsgClient.send_text",
        return_value={"sent": "true", "id": "provider-modification-1"},
    ) as send:
        result = send_modification_payment_link_request(modification_id=modification.pk)

    assert result.code == "sent"
    assert send.call_count == 1
    assert "طلب إنشاء رابط دفع يدوي لفرق تعديل حجز" in send.call_args.kwargs["body"]
    assert result.delivery.status == WhatsAppDelivery.Status.SENT


@override_settings(
    ULTRAMSG_ENABLED=True,
    ULTRAMSG_API_BASE_URL="https://api.ultramsg.com",
    ULTRAMSG_INSTANCE_ID="instance-test",
    ULTRAMSG_TOKEN="test-token",
    ACCOUNTING_WHATSAPP_NUMBER="+966597193102",
)
def test_successful_modification_request_returns_to_booking_list():
    reservation = make_reservation("LSA-ULTRAMSG-MODIFICATION-2")
    modification = BookingModificationRequest.objects.create(
        reservation=reservation,
        request_type=BookingModificationRequest.RequestType.CHANGE_DATES,
        status=BookingModificationRequest.Status.COMPLETED,
        completed_at=timezone.now(),
        old_check_in=reservation.check_in,
        old_check_out=reservation.check_out,
        new_check_in=reservation.check_in,
        new_check_out=reservation.check_out + timedelta(days=1),
        old_guests=reservation.guests,
        new_guests=reservation.guests,
        old_total=reservation.total_price,
        new_total=reservation.total_price + 50,
        price_difference=50,
        currency=reservation.currency,
        quote_snapshot={"owner_final_total": str(reservation.total_price + 50)},
        idempotency_key="ultramsg-modification-redirect-" + "x" * 32,
        session_key_hash="ultramsg-modification-redirect-session",
        expires_at=timezone.now() + timedelta(days=1),
    )
    reservation.check_out = modification.new_check_out
    reservation.total_price = modification.new_total
    reservation.normalized_status = "modified"
    reservation.save(update_fields=["check_out", "total_price", "normalized_status"])
    user = get_user_model().objects.create_superuser(
        username="ultramsg-operations-owner",
        email="owner@example.invalid",
        password="not-used",
    )
    client = Client()
    client.force_login(user)

    with patch(
        "apps.notifications.services.ultramsg.UltraMsgClient.send_text",
        return_value={"sent": "true", "id": "provider-modification-2"},
    ):
        response = client.post(
            reverse("notifications:booking_detail", args=[reservation.pk]),
            {
                "action": "send_modification_payment_link_request",
                "modification_id": modification.pk,
            },
        )

    assert response.status_code == 302
    assert response.url == reverse("notifications:booking_list")
