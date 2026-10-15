"""Durable guest delivery and gateway recovery on the existing worker."""

from celery import shared_task
from django.conf import settings
from django.db import transaction
from django.utils import timezone, translation

from apps.core.phone_numbers import normalize_phone_number
from apps.notifications.services.ultramsg import UltraMsgClient

from .models import PaymentRequestDelivery, ReservationPaymentRequest

PAYMENT_MESSAGE_BRAND = "Luxury Smart Apartments"


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
    with translation.override(language):
        dates = f"{localized_date(reservation.check_in)} — {localized_date(reservation.check_out)}"
        amount = localized_money_text(request.amount_sar, "SAR", language=language)
        if language == "ar":
            amount = localized_money_text(request.amount_sar, "", language=language) + " ريال سعودي"
    blocks = [
        f"✨ *{brand}*",
        greeting,
        intro if kind == "link" else f"✅ {receipt}",
        f"📅 *{stay}:* {dates}\n💳 *{total}:* {amount}",
    ]
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
        with UltraMsgClient() as client:
            result = client.send_text(
                recipient=recipient, body=body, reference_id=f"payment-request:{delivery.pk}"
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
        return result
