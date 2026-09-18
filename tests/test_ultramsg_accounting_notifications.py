"""The accountant gets one private WhatsApp request per manual reservation."""

from unittest.mock import patch

import pytest
from django.test import override_settings

from apps.notifications.models import WhatsAppDelivery
from apps.notifications.services.ultramsg import send_manual_payment_link_request
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
