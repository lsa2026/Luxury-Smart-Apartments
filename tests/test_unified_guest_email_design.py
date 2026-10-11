"""No external delivery: all messages use memory mail and synthetic snapshots."""

import re
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from allauth.account.adapter import get_adapter
from django.conf import settings
from django.core import mail
from django.test import override_settings
from django.utils import timezone
from django.utils.translation import override

from apps.notifications.email_design import COPY
from apps.notifications.models import GuestInvoice, GuestInvoiceDelivery
from apps.notifications.services.email import (
    MESSAGES,
    SUBJECTS,
    TEMPLATE_GROUPS,
    EmailMessageRequest,
    build_guest_email,
)
from apps.notifications.services.invoice_delivery import deliver_invoice
from apps.notifications.services.invoice_email import invoice_filename

pytestmark = pytest.mark.django_db


@pytest.mark.parametrize("language", ["ar", "en", "fr"])
@pytest.mark.parametrize("message_type", sorted(TEMPLATE_GROUPS))
@override_settings(
    EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
    DEFAULT_FROM_EMAIL="Luxury Living <noreply@example.invalid>",
    SITE_BASE_URL="https://stays.example.invalid",
    EMAIL_REPLY_TO="support@example.invalid",
)
def test_every_registered_message_has_one_approved_shell(language, message_type):
    message = build_guest_email(
        EmailMessageRequest(
            recipient="guest@example.invalid",
            subject=SUBJECTS[message_type][language],
            template_name=TEMPLATE_GROUPS[message_type],
            language=language,
            context={
                "heading": SUBJECTS[message_type][language],
                "message": MESSAGES[message_type][language],
                "first_name": "<script>Guest</script>",
                "reference": "TEST-BOOK-123",
                "action_url": "https://stays.example.invalid/verify/?token=test-only",
                "manage_url": "https://stays.example.invalid/reservations/manage/",
                "verification_code": "123456",
                "expires_in_minutes": 15,
                "property_name": "Test apartment",
                "check_in": "10/10/2026",
                "check_out": "15/10/2026",
                "guests": 2,
            },
        )
    )
    html = message.alternatives[0].content
    direction = "rtl" if language == "ar" else "ltr"
    assert f'<html lang="{language}" dir="{direction}">' in html
    assert html.count("luxury-living-lockup-light.png") == 1
    assert "apple-touch-icon" not in html
    assert COPY[language]["help"] in html
    assert "<script>Guest</script>" not in html
    assert message.to == ["guest@example.invalid"]
    assert message.reply_to == ["support@example.invalid"]
    assert message.body and message.alternatives[0].mimetype == "text/html"
    if TEMPLATE_GROUPS[message_type] == "account":
        assert "123456" in html and "token=test-only" in html


@pytest.mark.parametrize("language", ["ar", "en", "fr"])
@pytest.mark.parametrize("signup", [False, True])
def test_allauth_confirmation_preserves_link_and_localizes_shared_shell(language, signup):
    prefix = "account/email/email_confirmation" + ("_signup" if signup else "")
    with override(language):
        message = get_adapter().render_mail(
            prefix,
            "guest@example.invalid",
            {"activate_url": "https://stays.example.invalid/confirm/test-only-token/"},
        )
    html = message.alternatives[0].content
    assert "luxury-living-lockup-light.png" in html
    assert f'lang="{language}"' in html
    assert COPY[language]["confirmation_action"] in html
    assert message.subject == COPY[language]["confirmation_subject"]
    assert "test-only-token" in html and "test-only-token" in message.body
    assert COPY[language]["confirmation_notice"] in message.body


@pytest.mark.parametrize(
    "reference,expected",
    [
        ("5851373872", "Invoice-5851373872.pdf"),
        ("HMABC12345", "Invoice-HMABC12345.pdf"),
        ("BOOK-ABC_123", "Invoice-BOOK-ABC_123.pdf"),
        ("５８５１", "Invoice-5851.pdf"),
        ("../guest/report", "Invoice-guest-report.pdf"),
        ('a"b', "Invoice-a-b.pdf"),
        ("bad\r\nX-Test: yes", "Invoice.pdf"),
        ("123456", "Invoice.pdf"),
        ("125670-123-2000-987", "Invoice.pdf"),
        ("", "Invoice.pdf"),
        ("عربي", "Invoice.pdf"),
    ],
)
def test_invoice_filename_never_exposes_internal_id_or_unsafe_header(reference, expected):
    invoice = SimpleNamespace(
        snapshot={"platform_reference": reference},
        hostaway_reservation_id=123456,
    )
    filename = invoice_filename(invoice)
    assert filename == expected
    assert re.fullmatch(r"Invoice(?:-[A-Za-z0-9_-]+)?\.pdf", filename)


@pytest.mark.parametrize("language", ["ar", "en", "fr"])
@override_settings(
    EMAIL_DELIVERY_ENABLED=True,
    EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
    DEFAULT_FROM_EMAIL="noreply@example.invalid",
)
def test_invoice_email_has_html_frozen_pdf_and_short_filename(language, django_user_model):
    actor = django_user_model.objects.create_user(username="owner@example.invalid")
    snapshot = {
        "platform_reference": "5851373872",
        "platform": "bookingcom",
        "guest_name": "Test Guest",
        "property_names": {language: "Test apartment"},
        "check_in": "2026-10-10",
        "check_out": "2026-10-15",
        "nights": 5,
        "total": "5788.28",
        "currency": "SAR",
    }
    frozen = b"%PDF-synthetic-immutable-test-only"
    invoice = GuestInvoice.objects.create(
        hostaway_reservation_id=123456,
        snapshot=snapshot,
        language=language,
        number="LL-2026-INTERNAL-DO-NOT-SHOW",
        created_by=actor,
        issued_at=timezone.now(),
        pdf_content=frozen,
    )
    delivery = GuestInvoiceDelivery.objects.create(
        invoice=invoice,
        channel="email",
        recipient="alternative@example.invalid",
        requested_by=actor,
    )
    assert deliver_invoice(delivery.pk) == "accepted"
    assert deliver_invoice(delivery.pk) == "accepted"
    assert len(mail.outbox) == 1
    message = mail.outbox[0]
    html = message.alternatives[0].content
    assert "luxury-living-lockup-light.png" in html
    assert "5,788.28 SAR" in html and "Booking.com" in html and "5851373872" in html
    assert "123456" not in html and "INTERNAL-DO-NOT-SHOW" not in html
    assert "123456" not in message.body
    assert "/admin/" not in html
    assert message.to == ["alternative@example.invalid"]
    assert message.attachments[0].filename == "Invoice-5851373872.pdf"
    assert message.attachments[0].content == frozen
    invoice.refresh_from_db()
    assert invoice.snapshot == snapshot and bytes(invoice.pdf_content) == frozen
    assert invoice.number == "LL-2026-INTERNAL-DO-NOT-SHOW"


@override_settings(ULTRAMSG_ENABLED=True)
def test_whatsapp_filename_uses_the_same_rule_without_resending(django_user_model):
    actor = django_user_model.objects.create_user(username="owner@example.invalid")
    invoice = GuestInvoice.objects.create(
        hostaway_reservation_id=123456,
        snapshot={"platform_reference": "HMTEST123"},
        created_by=actor,
        issued_at=timezone.now(),
        pdf_content=b"%PDF-test-only",
    )
    delivery = GuestInvoiceDelivery.objects.create(
        invoice=invoice,
        channel="whatsapp",
        recipient="+12025550100",
        requested_by=actor,
    )
    with patch("apps.notifications.services.invoice_delivery.UltraMsgClient") as factory:
        document = factory.return_value.__enter__.return_value.send_document
        assert deliver_invoice(delivery.pk) == "accepted"
        assert deliver_invoice(delivery.pk) == "accepted"
        document.assert_called_once()
        assert document.call_args.kwargs["filename"] == "Invoice-HMTEST123.pdf"


def test_static_email_logo_is_the_exact_approved_pdf_lockup():
    original = Path(settings.BASE_DIR) / "apps/notifications/invoice_assets/lockup-light.png"
    email_logo = Path(settings.BASE_DIR) / "static/images/brand/luxury-living-lockup-light.png"
    assert original.read_bytes() == email_logo.read_bytes()
