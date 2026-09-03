from unittest.mock import patch

import pytest
from django.core import mail
from django.test import override_settings

from apps.core.models import SiteSetting
from apps.notifications.models import EmailDelivery
from apps.notifications.services.email import DjangoEmailProvider, EmailMessageRequest
from apps.notifications.services.events import handle_modification_completed
from apps.reservations.models import BookingModificationRequest
from tests.test_booking_modifications_phase6 import confirmed_reservation, create_extension

pytestmark = pytest.mark.django_db


@override_settings(
    EMAIL_DELIVERY_ENABLED=True,
    EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
    DEFAULT_FROM_EMAIL="noreply@example.invalid",
    SUPPORT_EMAIL="care@example.invalid",
    SITE_BASE_URL="https://stays.example.invalid",
    EMAIL_LOGO_URL="",
)
def test_customer_email_uses_live_brand_logo_site_and_contact() -> None:
    setting = SiteSetting.objects.first() or SiteSetting(site_name="Luxury Smart Apartments")
    setting.brand_name_ar = "اسم عربي يجب تجاهله"
    setting.contact_phone = "+966500000000"
    setting.save()

    DjangoEmailProvider().send(
        EmailMessageRequest(
            recipient="guest@example.invalid",
            subject="تأكيد الحجز",
            template_name="reservation",
            language="ar",
            context={
                "heading": "تم تأكيد الحجز",
                "message": "حجزك مؤكد.",
                "reference": "BOOK-100",
                "manage_url": "https://stays.example.invalid/reservations/manage/",
            },
        )
    )

    html = mail.outbox[0].alternatives[0].content
    text = mail.outbox[0].body
    assert "Luxury Smart Apartments" in html
    assert "اسم عربي يجب تجاهله" not in html
    assert 'src="https://stays.example.invalid/static/images/logo.jpeg"' in html
    assert "+966500000000" in html
    assert "https://stays.example.invalid/" in html
    assert "care@example.invalid" in html
    assert "+966500000000" in text


@override_settings(
    MODIFICATION_NOTIFICATION_EMAIL_ENABLED=True,
    EMAIL_DELIVERY_ENABLED=False,
)
def test_completed_modification_queues_one_final_customer_email() -> None:
    reservation = confirmed_reservation()
    modification = create_extension(reservation).request
    assert modification is not None
    modification.status = BookingModificationRequest.Status.COMPLETED
    modification.save(update_fields=["status", "updated_at"])

    handle_modification_completed(modification.pk)
    handle_modification_completed(modification.pk)

    delivery = EmailDelivery.objects.get(
        idempotency_key=f"modification-completed:{modification.public_reference}"
    )
    assert delivery.message_type == "reservation_modified"
    assert delivery.recipient_source == "modification"


@override_settings(
    MODIFICATION_NOTIFICATION_EMAIL_ENABLED=True,
    EMAIL_DELIVERY_ENABLED=False,
)
def test_completed_cancellation_queues_cancellation_email() -> None:
    reservation = confirmed_reservation()
    modification = create_extension(reservation).request
    assert modification is not None
    modification.request_type = BookingModificationRequest.RequestType.CANCEL_RESERVATION
    modification.status = BookingModificationRequest.Status.COMPLETED
    modification.save(update_fields=["request_type", "status", "updated_at"])

    with patch("apps.notifications.services.events.queue_email") as queue:
        handle_modification_completed(modification.pk)

    assert queue.call_args.kwargs["message_type"] == "reservation_cancelled"
    assert queue.call_args.kwargs["recipient"] == reservation.booking_intent.guest_email
