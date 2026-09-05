from unittest.mock import patch

import pytest
from django.core import mail
from django.test import override_settings
from django.utils import timezone

from apps.core.models import SiteSetting
from apps.notifications.models import EmailDelivery
from apps.notifications.services.email import (
    DjangoEmailProvider,
    EmailMessageRequest,
    send_queued_email,
)
from apps.notifications.services.events import (
    handle_modification_completed,
    handle_modification_created,
)
from apps.payments.models import PaymentAttempt
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
    setting.tagline_ar = "ضيافة فاخرة في كل إقامة"
    setting.contact_email = ""
    setting.contact_phone = "+966500000000"
    setting.whatsapp_display_number = "+966501234567"
    setting.whatsapp_url = ""
    setting.instagram_url = "https://instagram.com/luxury-stays"
    setting.facebook_url = "https://facebook.com/luxury-stays"
    setting.x_url = "https://x.com/luxury-stays"
    setting.linkedin_url = "https://linkedin.com/company/luxury-stays"
    setting.office_hours_ar = "يوميًا من 9 صباحًا إلى 11 مساءً"
    setting.public_address_ar = "الرياض، المملكة العربية السعودية"
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
    assert 'dir="rtl"' in html
    assert "ضيافة فاخرة في كل إقامة" in html
    assert "محادثة فريق الدعم عبر واتساب" in html
    assert "https://wa.me/966501234567?text=" in html
    assert "https://instagram.com/luxury-stays" in html
    assert "https://facebook.com/luxury-stays" in html
    assert "https://x.com/luxury-stays" in html
    assert "https://linkedin.com/company/luxury-stays" in html
    assert "يوميًا من 9 صباحًا إلى 11 مساءً" in html
    assert "الرياض، المملكة العربية السعودية" in html
    assert "https://stays.example.invalid/" in html
    assert "care@example.invalid" in html
    assert "+966500000000" in text
    assert "https://wa.me/966501234567?text=" in text
    assert "https://instagram.com/luxury-stays" in text
    assert "https://stays.example.invalid/contact/" in text


@pytest.mark.parametrize(
    ("language", "direction", "tagline", "support_heading"),
    [
        ("ar", "rtl", "إقامة عربية راقية", "نحن هنا لخدمتك"),
        ("en", "ltr", "An exceptional English stay", "We are here to help"),
        ("fr", "ltr", "Un séjour français exceptionnel", "Nous sommes à votre écoute"),
    ],
)
@override_settings(
    EMAIL_DELIVERY_ENABLED=True,
    EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
    DEFAULT_FROM_EMAIL="Luxury Smart Apartments <notifications@example.invalid>",
    EMAIL_REPLY_TO="care@example.invalid",
    SUPPORT_EMAIL="care@example.invalid",
    SITE_BASE_URL="https://stays.example.invalid",
    EMAIL_LOGO_URL="",
)
def test_customer_email_localizes_template_direction_and_support(
    language: str,
    direction: str,
    tagline: str,
    support_heading: str,
) -> None:
    setting = SiteSetting.objects.first() or SiteSetting(site_name="Luxury Smart Apartments")
    setting.tagline_ar = "إقامة عربية راقية"
    setting.tagline_en = "An exceptional English stay"
    setting.tagline_fr = "Un séjour français exceptionnel"
    setting.contact_email = "guestcare@example.invalid"
    setting.whatsapp_display_number = "+966501234567"
    setting.instagram_url = "https://instagram.com/luxury-stays"
    setting.save()

    DjangoEmailProvider().send(
        EmailMessageRequest(
            recipient="guest@example.invalid",
            subject="Luxury Smart Apartments",
            template_name="contact",
            language=language,
            context={"heading": "Luxury Smart Apartments", "message": "Test message"},
        )
    )

    html = mail.outbox[0].alternatives[0].content
    assert f'<html lang="{language}" dir="{direction}">' in html
    assert tagline in html
    assert support_heading in html
    assert "guestcare@example.invalid" in html
    assert "https://wa.me/966501234567?text=" in html
    assert "https://instagram.com/luxury-stays" in html


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
    assert delivery.template_name == "modification"


@override_settings(
    MODIFICATION_NOTIFICATION_EMAIL_ENABLED=True,
    EMAIL_DELIVERY_ENABLED=True,
)
def test_created_modification_records_no_customer_receipt_email() -> None:
    reservation = confirmed_reservation()
    modification = create_extension(reservation).request
    assert modification is not None

    handle_modification_created(modification.pk)

    assert not EmailDelivery.objects.filter(
        idempotency_key=f"modification-created:{modification.public_reference}"
    ).exists()


@override_settings(
    MODIFICATION_NOTIFICATION_EMAIL_ENABLED=True,
    EMAIL_DELIVERY_ENABLED=False,
)
def test_verified_payment_does_not_send_final_email_before_hostaway_completion() -> None:
    reservation = confirmed_reservation()
    modification = create_extension(reservation).request
    assert modification is not None
    PaymentAttempt.objects.create(
        booking_intent=reservation.booking_intent,
        modification_request=modification,
        provider="hyperpay",
        amount=modification.price_difference,
        currency=modification.currency,
        status=PaymentAttempt.Status.SUCCEEDED,
        verified_at=timezone.now(),
        idempotency_key="modification-email-paid-not-complete-0000000000",
    )

    handle_modification_completed(modification.pk)

    assert not EmailDelivery.objects.filter(
        idempotency_key=f"modification-completed:{modification.public_reference}"
    ).exists()


@pytest.mark.parametrize(
    ("language", "type_label", "payment_label"),
    [
        ("ar", "تمديد الإقامة", "المبلغ المدفوع للتعديل"),
        ("en", "Stay extension", "Amount paid for this update"),
        ("fr", "Prolongation du séjour", "Montant payé pour cette modification"),
    ],
)
@override_settings(
    MODIFICATION_NOTIFICATION_EMAIL_ENABLED=True,
    EMAIL_DELIVERY_ENABLED=False,
    EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
    DEFAULT_FROM_EMAIL="Luxury Smart Apartments <notifications@example.invalid>",
    SUPPORT_EMAIL="care@example.invalid",
    SITE_BASE_URL="https://stays.example.invalid",
    EMAIL_LOGO_URL="",
)
def test_completed_paid_extension_email_contains_verified_before_and_after_details(
    language: str,
    type_label: str,
    payment_label: str,
) -> None:
    reservation = confirmed_reservation()
    intent = reservation.booking_intent
    intent.language = language
    intent.save(update_fields=["language", "updated_at"])
    modification = create_extension(reservation).request
    assert modification is not None
    PaymentAttempt.objects.create(
        booking_intent=intent,
        modification_request=modification,
        provider="hyperpay",
        amount=modification.price_difference,
        currency=modification.currency,
        status=PaymentAttempt.Status.SUCCEEDED,
        verified_at=timezone.now(),
        idempotency_key=f"modification-email-render-{language}-000000000000000",
    )
    modification.status = BookingModificationRequest.Status.COMPLETED
    modification.completed_at = timezone.now()
    modification.save(update_fields=["status", "completed_at", "updated_at"])
    handle_modification_completed(modification.pk)
    delivery = EmailDelivery.objects.get(
        idempotency_key=f"modification-completed:{modification.public_reference}"
    )
    delivery.status = EmailDelivery.Status.QUEUED
    delivery.save(update_fields=["status", "updated_at"])

    with override_settings(EMAIL_DELIVERY_ENABLED=True):
        result = send_queued_email(delivery.pk)

    assert result.sent is True
    html = mail.outbox[-1].alternatives[0].content
    text = mail.outbox[-1].body
    assert type_label in html
    assert payment_label in html
    assert modification.old_check_in.strftime("%d/%m/%Y") in html
    assert modification.old_check_out.strftime("%d/%m/%Y") in html
    assert modification.new_check_out.strftime("%d/%m/%Y") in html
    assert f"{modification.old_total:,.2f}" in html
    assert f"{modification.new_total:,.2f}" in html
    assert f"{modification.price_difference:,.2f}" in html
    assert modification.currency in html
    assert reservation.public_reference in html
    assert modification.public_reference in html
    assert payment_label in text


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
