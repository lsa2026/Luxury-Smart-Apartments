"""Synthetic owner-only catalogue. Never creates OTPs, memberships or money facts."""

import hashlib
from datetime import date
from decimal import Decimal
from hmac import compare_digest
from types import SimpleNamespace
from uuid import UUID

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone
from django.utils.translation import override

from apps.accounts.models import VerifiedPhone
from apps.accounts.phone_messages import phone_logo, phone_message
from apps.notifications.models import AuditLog
from apps.notifications.services.ultramsg import UltraMsgClient
from apps.payments.payment_link_tasks import payment_message

from .invitation_design import invitation_caption, invitation_image
from .models import GuestEvent, Ledger, Member
from .whatsapp_notices import lifecycle_caption, movement_caption, phone_digest

# Exact owner-approved E.164 destination, not a suffix or an editable profile field.
# Retain only its digest in source; a changed destination needs explicit confirmation.
OWNER_PREVIEW_PHONE_SHA256 = "cd8af36a4b176ad5b18b7cd6d93f501872c16a70f9eb14ecc39e90485b9ebd2b"


def catalogue(language="ar"):
    if language not in {"ar", "en", "fr"}:
        raise ValidationError("Unsupported preview language.")
    note = {
        "ar": "معاينة للمالك فقط — بيانات افتراضية؛ لا رمز صالح ولا تغيير في نقاطك أو حجوزاتك.",
        "en": "Owner preview only — fictional data, no valid code or account changes.",
        "fr": "Aperçu propriétaire — données fictives, aucun code valide "
        "ni modification du compte.",
    }[language]
    result = []

    def add(key, body, content=None):
        caption = note + "\n\n" + body
        if len(caption.encode("utf-16-le")) // 2 > 1024:
            raise ValidationError("Preview caption too long.")
        result.append((key, caption, content or phone_logo()))

    for purpose in ("bind", "login"):
        add("auth-" + purpose, phone_message(code="000000", language=language, purpose=purpose))
    name = {"ar": "ضيف افتراضي", "en": "Sample Guest", "fr": "Client fictif"}[language]
    add(
        "invitation",
        invitation_caption(
            name,
            language=language,
            url=settings.SITE_BASE_URL.rstrip("/") + "/my-bookings/rewards/",
        ),
        invitation_image(language),
    )
    for key, operation, event, debit, value in [
        ("stay", "loyalty", "earn", False, "28.71"),
        ("direct", "direct_referral", "earn", False, "10.00"),
        ("earlier", "indirect_referral", "earn", False, "5.00"),
        ("addition", "addition", "earn", False, "5.00"),
        ("gift-out", "transfer", "transfer", True, "3.00"),
        ("gift-in", "transfer", "transfer", False, "3.00"),
        ("redeem", "transfer", "redeem", True, "25.00"),
        ("restore", "addition", "restore", False, "25.00"),
        ("correction", "addition", "adjust", True, "4.50"),
        ("negative", "addition", "adjust", True, "28.71"),
        ("close-positive", "transfer", "withdraw", True, "25.71"),
    ]:
        balance = "-3.00" if key == "negative" else "0.00" if event == "withdraw" else "25.71"
        row = Ledger(
            operation_type=operation,
            event_type=event,
            credit=Decimal("0") if debit else Decimal(value),
            debit=Decimal(value) if debit else Decimal("0"),
            member_balance_after=Decimal(balance),
            operation_number="PREVIEW-01",
            rate_percent=Decimal("3") if key == "stay" else None,
            occurred_at=timezone.now(),
            rules_version="preview-only",
        )
        party = {"name": name, "number": "PREVIEW-MEMBER"} if event == "transfer" else None
        add(key, movement_caption(row, language, preview_counterpart=party))
    for kind in (GuestEvent.Kind.JOIN, GuestEvent.Kind.CLOSE, GuestEvent.Kind.READY):
        event = GuestEvent(kind=kind, occurred_at=timezone.now())
        event.member = Member(number="PREVIEW-MEMBER")
        add(kind, lifecycle_caption(event, language))
    request = SimpleNamespace(
        pk=UUID(int=0),
        language=language,
        amount_sar=Decimal("950.00"),
        reservation=SimpleNamespace(check_in=date(2026, 12, 15), check_out=date(2026, 12, 16)),
    )
    for kind in ("link", "receipt"):
        add("payment-" + kind, payment_message(request, kind))
    with override(language):
        from apps.notifications.invoice_pdf import render_invoice_pdf
        from apps.notifications.services.invoice_delivery import MESSAGES

        invoice = SimpleNamespace(
            language=language,
            issued_at=None,
            snapshot={
                "supplier": name,
                "website": settings.SITE_BASE_URL,
                "guest_name": name,
                "guest_email": "preview@example.invalid",
                "guest_phone": "—",
                "platform": "website",
                "platform_reference": "PREVIEW-ONLY",
                "property_names": {
                    "ar": "شقة افتراضية — للمراجعة فقط",
                    "en": "Preview apartment",
                    "fr": "Appartement fictif",
                },
                "check_in": "2026-12-15",
                "check_out": "2026-12-16",
                "nights": 1,
                "total": "950.00",
                "currency": "SAR",
            },
        )
        result.append(
            ("invoice", note + "\n\n" + MESSAGES[language][1], render_invoice_pdf(invoice))
        )
    return result


def send_catalogue(*, batch, language="ar"):
    """No recipient override; only the owner's existing verified membership phone."""
    owner = Member.objects.select_related("customer").get(number="LSA-M000001")
    if not owner.customer.is_active:
        raise ValidationError("Owner account inactive.")
    binding = VerifiedPhone.objects.get(user=owner.customer)
    if not compare_digest(
        hashlib.sha256(binding.phone.encode()).hexdigest(), OWNER_PREVIEW_PHONE_SHA256
    ):
        raise ValidationError("Owner destination requires confirmation.")
    packets = catalogue(language)
    results = []
    for key, caption, content in packets:
        reference = f"{batch}:{language}:{key}"
        if len(reference) > 100:
            raise ValidationError("Preview reference too long.")
        with transaction.atomic():
            get_user_model().objects.select_for_update().get(pk=owner.customer_id)
            if AuditLog.objects.filter(
                action="whatsapp.preview.started",
                object_reference=reference,
            ).exists():
                results.append((key, "already_attempted"))
                continue
            if not VerifiedPhone.objects.filter(
                pk=binding.pk,
                version=binding.version,
                phone=binding.phone,
                user_id=owner.customer_id,
            ).exists():
                raise ValidationError("Owner phone changed.")
            AuditLog.objects.create(
                action="whatsapp.preview.started",
                object_type="owner_message_preview",
                object_reference=reference,
                summary="معاينة قالب مفوضة من المالك",
                metadata={
                    "language": language,
                    "kind": key,
                    "phone_digest": phone_digest(binding.phone),
                    "caption_digest": hashlib.sha256(caption.encode()).hexdigest(),
                },
            )
        try:
            with UltraMsgClient() as client:
                if key == "invoice":
                    client.send_document(
                        recipient=binding.phone,
                        content=content,
                        caption=caption,
                        filename="preview-only.pdf",
                    )
                else:
                    client.send_image(
                        recipient=binding.phone,
                        content=content,
                        caption=caption,
                        reference_id="owner-preview:" + reference,
                    )
        except Exception:
            AuditLog.objects.create(
                action="whatsapp.preview.uncertain",
                object_type="owner_message_preview",
                object_reference=reference,
                summary="نتيجة المعاينة غير مؤكدة؛ لا إعادة تلقائية",
            )
            results.append((key, "uncertain"))
            break
        AuditLog.objects.create(
            action="whatsapp.preview.accepted",
            object_type="owner_message_preview",
            object_reference=reference,
            summary="قبل المزود معاينة القالب؛ الوصول غير مؤكد",
        )
        results.append((key, "accepted"))
    return results
