"""Read-only Hostaway lookup and isolated invoice issuance/delivery."""

import hashlib
import json
from decimal import Decimal, InvalidOperation

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from apps.integrations.hostaway.exceptions import HostawayResponseError
from apps.integrations.hostaway.reservation_validators import validate_reservation_response
from apps.notifications.invoice_forms import plain
from apps.notifications.models import GuestInvoice, GuestInvoiceDelivery
from apps.properties.models import Property


def snapshot_token(invoice):
    """Detect a refresh from another tab before issuing a newly changed amount."""
    data = json.dumps(invoice.snapshot, sort_keys=True, ensure_ascii=True, separators=(",", ":"))
    return hashlib.sha256(f"{invoice.pk}:{data}".encode()).hexdigest()


def invoice_snapshot(payload, expected_id):
    """Allowlist billing data only; never retain door codes, card data or raw response."""
    booking = validate_reservation_response(payload)
    if booking.reservation_id != expected_id:
        raise HostawayResponseError("Unexpected reservation identity.")
    if booking.status.casefold() not in {"new", "confirmed", "modified", "awaitingpayment"}:
        raise ValidationError("الحجز ملغى أو غير مؤكد؛ لا يمكن إصدار فاتورة إقامة له.")
    raw = payload["result"]
    property_obj = Property.objects.filter(
        Q(hostaway_listing_map_id=booking.listing_map_id)
        | Q(hostaway_listing_id=booking.listing_map_id)
    ).first()
    if property_obj is None:
        raise ValidationError("الشقة غير مرتبطة بعقاراتنا المحلية؛ يلزم مراجعتها قبل إصدار فاتورة.")
    try:
        total = Decimal(str(raw.get("totalPrice")))
        if not total.is_finite() or not Decimal("0") < total <= Decimal("99999999.99"):
            raise ValueError
        if total != total.quantize(Decimal("0.01")):
            raise ValueError
    except (InvalidOperation, ValueError) as exc:
        raise ValidationError("إجمالي الحجز غير صالح أو غير مكتمل؛ راجعه في المنصة أولًا.") from exc
    currency = booking.currency
    # Only our documented direct-booking labeling correction applies to direct stays.
    # External channel amounts must NOT silently change their currency interpretation.
    if booking.channel_id == 2000 and property_obj.price_currency_override:
        currency = property_obj.price_currency_override.upper()
    if currency not in {"SAR", "MAD", "USD", "EUR", "GBP", "AED", "KWD", "BHD", "QAR"}:
        raise ValidationError("عملة الحجز تحتاج مراجعة قبل إصدار الفاتورة.")
    # KWD/BHD have three minor digits and need a separately verified billing renderer.
    if currency in {"KWD", "BHD"}:
        raise ValidationError("هذه العملة تتطلب ثلاث خانات عشرية؛ يلزم دعمها قبل الإصدار.")
    platform = plain(raw.get("channelName") or raw.get("source"))[:80]
    # Hostaway documents confirmationCode as the guest's Airbnb confirmation code.
    # reservationId can be a composite INTERNAL identifier, not the guest's reference.
    reference = plain(
        (raw.get("confirmationCode") if "airbnb" in platform.casefold() else "")
        or raw.get("channelReservationId")
    )[:100]
    if reference == str(expected_id) or re_internal_reference(reference):
        reference = ""
    if booking.channel_id == 2000:
        platform = "Direct"
        # The locally assigned guest-facing reference is preferable for website bookings.
        from apps.reservations.models import Reservation

        local = Reservation.objects.filter(hostaway_reservation_id=expected_id).first()
        if local:
            reference = local.public_reference
        elif reference and re_internal_reference(reference):
            reference = ""
    locale = plain(raw.get("guestLocale")).lower().replace("_", "-").split("-")[0]
    names = {
        lang: plain(getattr(property_obj, f"name_{lang}", "") or property_obj.name_en)[:255]
        for lang in ("ar", "en", "fr")
    }
    return {
        "platform_reference": reference,
        "platform": platform or "External booking",
        "guest_name": plain(
            raw.get("guestName")
            or " ".join(
                filter(None, [plain(raw.get("guestFirstName")), plain(raw.get("guestLastName"))])
            )
        )[:160],
        "guest_email": plain(raw.get("guestEmail"))[:254],
        "guest_phone": plain(raw.get("phone"))[:30],
        "language": locale if locale in {"ar", "en", "fr"} else "ar",
        "property_names": names,
        "check_in": booking.check_in.isoformat(),
        "check_out": booking.check_out.isoformat(),
        "nights": (booking.check_out - booking.check_in).days,
        "total": format(total, ".2f"),
        "currency": currency,
        "supplier": "مؤسسة الحياة الذكية التجارية",
        "supplier_en": "Smart Life Trading Establishment",
        "website": "luxurysmartapartments.com",
        "retrieved_at": timezone.now().isoformat(),
    }


def re_internal_reference(value):
    import re

    return bool(re.fullmatch(r"\d+-\d+-(?:2000|thread\d+)-.+", value))


def prepare_invoice(*, client, hostaway_id, actor):
    existing = GuestInvoice.objects.filter(hostaway_reservation_id=hostaway_id).first()
    if existing and existing.issued_at:
        return existing
    snapshot = invoice_snapshot(client.get_reservation_invoice_data(hostaway_id), hostaway_id)
    with transaction.atomic():
        invoice, _ = GuestInvoice.objects.get_or_create(
            hostaway_reservation_id=hostaway_id,
            defaults={"created_by": actor, "snapshot": snapshot, "language": snapshot["language"]},
        )
        invoice = GuestInvoice.objects.select_for_update().get(pk=invoice.pk)
        if not invoice.issued_at:
            invoice.snapshot = snapshot
            invoice.language = snapshot["language"]
            invoice.save(update_fields=["snapshot", "language"])
        return invoice


def issue_invoice(invoice_id, reviewed):
    from apps.notifications.invoice_pdf import render_invoice_pdf

    with transaction.atomic():
        invoice = GuestInvoice.objects.select_for_update().get(pk=invoice_id)
        if invoice.issued_at:
            return invoice
        if reviewed.get("snapshot_token") != snapshot_token(invoice):
            raise ValidationError(
                "تغيّرت بيانات المسودة بعد فتحها؛ راجع المعاينة الجديدة قبل الإصدار."
            )
        invoice.snapshot = dict(invoice.snapshot)
        for key in ("platform_reference", "guest_name", "guest_email", "guest_phone"):
            invoice.snapshot[key] = reviewed[key]
        invoice.language = reviewed["language"]
        invoice.issued_at = timezone.now()
        invoice.number = f"LL-{timezone.localdate().year}-{invoice.pk.hex.upper()}"
        invoice.pdf_content = render_invoice_pdf(invoice)
        invoice.save()
        return invoice


def queue_invoice_delivery(*, invoice, channel, recipient, actor):
    if not invoice.issued_at or not invoice.pdf_content:
        raise ValidationError("أصدر الفاتورة أولًا قبل إرسالها.")
    if channel == "email" and not settings.EMAIL_DELIVERY_ENABLED:
        raise ValidationError("إرسال البريد غير مفعّل حاليًا؛ يمكنك تنزيل الفاتورة.")
    if channel == "whatsapp" and not settings.ULTRAMSG_ENABLED:
        raise ValidationError("اتصال واتساب غير مفعّل حاليًا؛ يمكنك تنزيل الفاتورة.")
    with transaction.atomic():
        delivery, created = GuestInvoiceDelivery.objects.get_or_create(
            invoice=invoice,
            channel=channel,
            recipient=recipient,
            defaults={"requested_by": actor},
        )
        delivery = GuestInvoiceDelivery.objects.select_for_update().get(pk=delivery.pk)
        # A second OWNER click may retry an explicitly rejected attempt, not an
        # accepted or ambiguous attempt. Preserve the attempt count for auditing.
        if not created and delivery.status == "failed":
            delivery.status = "queued"
            delivery.error_code = ""
            delivery.save(update_fields=["status", "error_code"])
            created = True
        if created:
            transaction.on_commit(lambda: dispatch_invoice_delivery(str(delivery.pk)))
    return delivery


def dispatch_invoice_delivery(delivery_id):
    from apps.notifications.tasks import send_guest_invoice_task

    try:
        send_guest_invoice_task.delay(delivery_id)
    except Exception:
        # Broker acknowledgement can be ambiguous: never re-send automatically.
        GuestInvoiceDelivery.objects.filter(pk=delivery_id, status="queued").update(
            status="review", error_code="queue_acknowledgement_unknown"
        )
