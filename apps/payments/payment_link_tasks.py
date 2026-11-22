"""Durable guest delivery and gateway recovery on the existing worker."""

import re

from celery import shared_task
from django.conf import settings
from django.db import transaction
from django.utils import timezone, translation

from apps.core.phone_numbers import normalize_phone_number
from apps.notifications.services.ultramsg import UltraMsgClient
from apps.notifications.whatsapp_style import BRAND

from .models import PaymentRequestDelivery, ReservationPaymentRequest

PAYMENT_MESSAGE_BRAND = BRAND
EXTENSION_PAYMENT_MESSAGE_CONTRACT = "confirmed_booking_link_v1"


def queue_delivery(request_id, *, kind="link", resend=False):
    from .payment_links import validate_open

    with transaction.atomic():
        request = ReservationPaymentRequest.objects.select_for_update().get(pk=request_id)
        if request.environment != settings.HYPERPAY_ENVIRONMENT:
            raise ValueError("payment_environment_mismatch")
        if kind == "link":
            validate_open(request)
        elif kind != "receipt" or request.status != "paid":
            raise ValueError("receipt_requires_paid_request")
        previous = request.deliveries.filter(kind=kind).order_by("-sequence").first()
        if previous:
            if not resend or kind == "receipt":
                return previous
            # Each explicit operator resend is a new, auditable delivery,
            # even after a failure/uncertain result or an immediate repeat.
            # Default/background calls remain idempotent; worker execution
            # still claims each delivery once. Never regenerate the pay link.
        delivery = PaymentRequestDelivery.objects.create(
            request=request, kind=kind, sequence=previous.sequence + 1 if previous else 1
        )
        transaction.on_commit(lambda: _dispatch(delivery.pk), robust=True)
        return delivery


def _dispatch(delivery_id):
    try:
        deliver_payment_request_task.delay(delivery_id)
    except Exception:
        # A queued database row can be recovered; never send synchronously as fallback.
        pass


def _extension_link_blocks(request, language, dates, amount):
    """Describe a verified child booking without claiming collection or paid activation."""
    reservation_id = getattr(request, "reservation_id", None)
    if reservation_id is None:
        # The existing message catalogue renders synthetic previews without a DB read.
        return None

    from apps.core.templatetags.presentation import (
        localized_date,
        localized_money_text,
        localized_property_name,
    )
    from apps.reservations.models import StayExtension
    from apps.reservations.stay_extensions import _checkout, booking_verified

    extension = (
        StayExtension.objects.select_related("draft", "chain__property")
        .filter(reservation_id=reservation_id)
        .first()
    )
    if extension is None:
        return None
    extension.reservation = request.reservation
    if not booking_verified(extension):
        raise ValueError("extension_booking_unverified")
    labels = {
        "ar": (
            "تم تأكيد إنشاء حجز تمديد إقامتك.",
            "الشقة",
            "الفترة الإضافية",
            "موعد الخروج الجديد",
            "المبلغ المطلوب سداده",
            "حالة السداد: بانتظار السداد.",
            "توقيت الشقة",
            "قيمة التمديد بعملة الحجز",
        ),
        "en": (
            "Your stay-extension booking has been confirmed.",
            "Apartment",
            "Additional stay",
            "New checkout",
            "Amount due",
            "Payment status: Awaiting payment.",
            "Property timezone",
            "Extension total in booking currency",
        ),
        "fr": (
            "La réservation de votre prolongation est confirmée.",
            "Appartement",
            "Période supplémentaire",
            "Nouveau départ",
            "Montant à régler",
            "Paiement : en attente de règlement.",
            "Fuseau horaire du logement",
            "Total de la prolongation dans la devise de réservation",
        ),
    }[language]
    confirmation, unit, period, checkout, total, pending, zone_label, source_total = labels
    checkout_at, zone_name = _checkout(request.reservation.property, request.reservation.check_out)
    with translation.override(language):
        name = re.sub(r"[\r\n*_~`]", " ", localized_property_name(request.reservation.property))
        blocks = [
            f"📌 {confirmation}",
            f"🏠 *{unit}:* {name}",
            f"📅 *{period}:* {dates}",
            f"🚪 *{checkout}:* {localized_date(checkout_at.date())} — {checkout_at:%H:%M}\n"
            f"{zone_label}: {zone_name}",
            f"💳 *{total}:* {amount}",
            pending,
        ]
        if request.source_currency != "SAR":
            blocks.insert(
                4,
                f"*{source_total}:* "
                + localized_money_text(
                    request.source_amount, request.source_currency, language=language
                ),
            )
    return blocks


def payment_message(request, kind="link", *, owner_review=False):
    """One branded text for queued guest delivery and explicit owner previews."""
    from apps.core.templatetags.presentation import localized_date, localized_money_text

    from .payment_links import public_url

    reservation = request.reservation
    language = request.language if request.language in {"ar", "en", "fr"} else "ar"
    copy = {
        "ar": (
            PAYMENT_MESSAGE_BRAND,
            "أهلًا بك،",
            "نود تزويدك برابط سداد حجزك:",
            "فترة الإقامة",
            "الإجمالي",
            "يمكنك إتمام السداد عبر الرابط الآمن أدناه:",
            "لا يتم تحصيل أي مبلغ بمجرد إرسال الرابط أو فتحه. "
            "يتم السداد فقط بعد تأكيد عملية الدفع.",
            f"شكرًا لاختياركم {PAYMENT_MESSAGE_BRAND}.",
            "تم استلام دفعتك بنجاح. لا تدفع مرة أخرى.",
            "نسخة مراجعة للمالك فقط، وليست رسالة للضيف.",
        ),
        "en": (
            PAYMENT_MESSAGE_BRAND,
            "Welcome,",
            "Here is the payment link for your booking:",
            "Stay",
            "Total",
            "Complete your payment using the secure link below:",
            "Sending or opening this link does not charge you. Payment requires your confirmation.",
            f"Thank you for choosing {PAYMENT_MESSAGE_BRAND}.",
            "Payment received. Please do not pay again.",
            "Owner review only — not a guest message.",
        ),
        "fr": (
            PAYMENT_MESSAGE_BRAND,
            "Bienvenue,",
            "Voici le lien de paiement de votre réservation :",
            "Séjour",
            "Total",
            "Effectuez votre paiement via le lien sécurisé ci-dessous :",
            "L’envoi ou l’ouverture du lien ne vous débite pas. "
            "Le paiement nécessite votre confirmation.",
            f"Merci d’avoir choisi {PAYMENT_MESSAGE_BRAND}.",
            "Paiement reçu. Ne payez pas à nouveau.",
            "Copie de contrôle du propriétaire uniquement — pas un message au client.",
        ),
    }[language]
    brand, greeting, intro, stay, total, cta, note, thanks, receipt, review = copy
    revised = bool(getattr(request, "modification_request_id", None))
    if revised:
        total = {
            "ar": "المبلغ المطلوب بالريال السعودي"
            if kind == "link"
            else "الدفعة المستلمة بالريال السعودي",
            "en": "Amount due in SAR" if kind == "link" else "Payment received in SAR",
            "fr": "Montant à régler en SAR" if kind == "link" else "Paiement reçu en SAR",
        }[language]
    with translation.override(language):
        date_in, date_out = reservation.check_in, reservation.check_out
        if revised and kind == "receipt":
            date_in = request.modification_request.new_check_in
            date_out = request.modification_request.new_check_out
        dates = f"{localized_date(date_in)} — {localized_date(date_out)}"
        amount = localized_money_text(request.amount_sar, "SAR", language=language)
        if language == "ar":
            amount = localized_money_text(request.amount_sar, "", language=language) + " ريال سعودي"
    extension_blocks = (
        _extension_link_blocks(request, language, dates, amount)
        if kind == "link" and not revised
        else None
    )
    blocks = [f"✨ *{brand}*", greeting]
    blocks.extend(
        extension_blocks
        if extension_blocks is not None
        else [
            intro if kind == "link" else f"✅ {receipt}",
            f"📅 *{stay}:* {dates}\n💳 *{total}:* {amount}",
        ]
    )
    if revised:
        snapshot = request.settlement_snapshot
        labels = {
            "ar": ("إجمالي الحجز المعدّل", "المدفوع الموثق", "المتبقي المطلوب سداده"),
            "en": ("Updated booking total", "Verified paid", "Remaining amount to pay"),
            "fr": ("Total du séjour modifié", "Déjà payé et vérifié", "Solde à régler"),
        }[language]
        if kind == "receipt":
            labels = (
                *labels[:2],
                {"ar": "الدفعة المستلمة الآن", "en": "Received now", "fr": "Reçu maintenant"}[
                    language
                ],
            )
        values = (snapshot["total"], snapshot["paid"], snapshot["due"])
        with translation.override(language):
            blocks.append(
                "\n".join(
                    f"*{label}:* "
                    + localized_money_text(value, request.source_currency, language=language)
                    for label, value in zip(labels, values, strict=True)
                )
            )
    if kind == "link":
        blocks.extend([f"🔗 {cta}", public_url(request), note])
    blocks.append(thanks)
    body = "\n\n".join(blocks)
    if owner_review:
        body = review + "\n\n" + body
    if settings.HYPERPAY_ENVIRONMENT == "test":
        body = "TEST / تجربة فقط — لا تحصيل حقيقي\n" + body
    return body


@shared_task(
    name="apps.payments.payment_link_tasks.deliver_payment_request_task",
    soft_time_limit=45,
    time_limit=60,
)
def deliver_payment_request_task(delivery_id):
    from apps.reservations.telegram_workgroup import manual_mode as workgroup_enabled

    if workgroup_enabled():
        from apps.reservations.telegram_website_workgroup import deliver_payment

        return deliver_payment(delivery_id)
    from .payment_links import validate_open

    with transaction.atomic():
        delivery = PaymentRequestDelivery.objects.select_for_update().get(pk=delivery_id)
        if delivery.status != "queued":
            return {"status": delivery.status}
        request = ReservationPaymentRequest.objects.select_related(
            "reservation__booking_intent"
        ).get(pk=delivery.request_id)
        try:
            if request.environment != settings.HYPERPAY_ENVIRONMENT:
                raise ValueError("payment_environment_mismatch")
            if delivery.kind == "link":
                if not settings.RESERVATION_PAYMENT_LINKS_ENABLED:
                    return {"status": "disabled"}
                validate_open(request)
            elif request.status != "paid":
                raise ValueError("not_paid")
            recipient = normalize_phone_number(request.reservation.booking_intent.guest_phone)
            body = payment_message(request, delivery.kind)
        except ValueError:
            delivery.status, delivery.error_code = "failed", "request_not_deliverable"
            delivery.save(update_fields=["status", "error_code"])
            return {"status": "failed"}
        if not settings.ULTRAMSG_ENABLED:
            return {"status": "disabled"}
        # Commit the claim before the external side effect. Unknown results are
        # not retried automatically, including a process crash while sending.
        delivery.status = "sending"
        delivery.save(update_fields=["status"])
    try:
        from apps.accounts.phone_messages import phone_logo

        with UltraMsgClient() as client:
            result = client.send_image(
                recipient=recipient,
                content=phone_logo(),
                caption=body,
                reference_id=f"payment-request:{delivery.pk}",
            )
        provider_id = str(result.get("id") or "")
        if not provider_id:
            raise ValueError("delivery_reference_missing")
    except Exception:
        PaymentRequestDelivery.objects.filter(pk=delivery.pk).update(
            status="unknown", error_code="delivery_outcome_uncertain"
        )
        return {"status": "unknown"}
    PaymentRequestDelivery.objects.filter(pk=delivery.pk).update(
        status="sent", provider_message_id=provider_id[:255], sent_at=timezone.now()
    )
    return {"status": "sent"}


@shared_task(
    name="apps.payments.payment_link_tasks.recover_payment_requests_task",
    soft_time_limit=150,
    time_limit=180,
)
def recover_payment_requests_task():
    from apps.integrations.tasks import distributed_task_lock

    from .payment_links import reconcile_requests

    with distributed_task_lock("reservation-payment-request-recovery") as acquired:
        if not acquired:
            return {"status": "already_running"}
        result = reconcile_requests()
        from .morning_payments import reconcile_mornings

        reconcile_mornings()
        from django.conf import settings

        if settings.DAY_USE08_ENABLED:
            from .day_use_payments import reconcile_day_uses

            reconcile_day_uses()
        # Recover a durable bookkeeping request if broker dispatch failed after payment.
        from django.db.models import Q

        from .hostaway_ledger import queue_payment_receipt
        from .models import PaymentAttempt

        for attempt in PaymentAttempt.objects.filter(
            payment_request__status="paid",
            payment_request__environment=settings.HYPERPAY_ENVIRONMENT,
            status="succeeded",
        ).filter(Q(hostaway_receipt__isnull=True) | Q(hostaway_receipt__status="pending"))[:3]:
            queue_payment_receipt(attempt.pk)
        for request in ReservationPaymentRequest.objects.filter(
            status="paid", environment=settings.HYPERPAY_ENVIRONMENT
        ).exclude(deliveries__kind="receipt")[:3]:
            queue_delivery(request.pk, kind="receipt")
        for delivery_id in (
            PaymentRequestDelivery.objects.filter(
                status="queued", request__environment=settings.HYPERPAY_ENVIRONMENT
            )
            .order_by("created_at")
            .values_list("pk", flat=True)[:3]
        ):
            deliver_payment_request_task.run(delivery_id)
        from apps.reservations.stay_extensions import recover_pending

        recover_pending()
        return result
