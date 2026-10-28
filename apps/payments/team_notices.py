"""One team receipt per new verified website capture, across every checkout path."""

import logging
from datetime import timedelta

from celery import shared_task
from django.conf import settings
from django.db import transaction
from django.utils import timezone

from apps.notifications.services.ultramsg import UltraMsgClient
from apps.notifications.whatsapp_style import BRAND
from apps.reservations.models import Reservation
from apps.reservations.morning_stays import property_zone

from .models import PaymentTeamNotice

logger = logging.getLogger(__name__)


def enabled():
    return settings.PAYMENT_TEAM_NOTICES_ENABLED and settings.HYPERPAY_ENVIRONMENT == "production"


def verified(attempt):
    return bool(
        attempt.status == "succeeded"
        and attempt.verified_at
        and attempt.provider_payment_id
        and attempt.amount > 0
        and attempt.currency == "SAR"
    )


def queue_notice(attempt, *, morning=False):
    """Called only on a NEW verified success, within its verification transaction.

    No scan of historical payments. Secondary delivery failures never undo money.
    A savepoint also isolates outbox failures from the financial transaction.
    """
    if not enabled() or not verified(attempt):
        return
    if morning:
        if attempt.stay.environment != "production":
            return
        key = {"morning_attempt_id": attempt.pk}
    else:
        if (
            attempt.provider != "hyperpay"
            or Reservation.objects.filter(
                booking_intent_id=attempt.booking_intent_id, is_test=True
            ).exists()
        ):
            return
        key = {"payment_attempt_id": attempt.pk}
    try:
        with transaction.atomic():
            notice, created = PaymentTeamNotice.objects.get_or_create(**key)
            if created:
                transaction.on_commit(lambda: dispatch(notice.pk), robust=True)
    except Exception:
        logger.exception("Team payment notice queue needs review")


def dispatch(notice_id):
    try:
        # Allow the payment completion to reconcile Hostaway before composing
        # the status. The receipt itself never depends on successful inventory writes.
        deliver_team_notice.apply_async(args=[str(notice_id)], countdown=15)
    except Exception:
        logger.exception("Team payment notice dispatch needs recovery")


def clean(value):
    return " ".join("".join(c for c in str(value) if c.isalnum() or c in " -_،").split())[:100]


def message(notice):
    if notice.morning_attempt_id:
        attempt = notice.morning_attempt
        stay = attempt.stay
        prop = stay.property
        zone = property_zone(prop)
        period = (
            f"{stay.starts_at.astimezone(zone):%Y-%m-%d %H:%M} ← "
            f"{stay.ends_at.astimezone(zone):%Y-%m-%d %H:%M} ({zone.key})"
        )
        guest, kind = stay.guest_name, "حجز صباحي"
        total, currency = stay.amount_sar, "SAR"
        reference = str(stay.pk)
        path = f"/admin/operations/morning-stays/{stay.pk}/"
        state = (
            "الحجز الصباحي مدفوع" if stay.status == "paid" else "يحتاج وضع الإقامة مراجعة الإدارة"
        )
    else:
        attempt = notice.payment_attempt
        intent = attempt.booking_intent
        prop = intent.property
        guest = f"{intent.guest_first_name} {intent.guest_last_name}"
        modification = attempt.modification_request
        request = attempt.payment_request
        if request and request.modification_request_id:
            modification = request.modification_request
        start, end, total, currency = (
            intent.check_in,
            intent.check_out,
            intent.total_price,
            intent.currency,
        )
        kind = "حجز جديد"
        if modification:
            start, end = modification.new_check_in, modification.new_check_out
            total, currency = modification.new_total, modification.currency
            kind = "سداد الحجز المعدّل أو المتبقي" if request else "سداد فرق تعديل الحجز"
        elif request:
            kind = "سداد حجز مؤكد من رابط الموقع"
        period = f"{start:%Y-%m-%d} ← {end:%Y-%m-%d}"
        reservation = Reservation.objects.filter(booking_intent_id=intent.pk).first()
        reference = reservation.public_reference if reservation else intent.public_reference
        path = (
            f"/admin/operations/bookings/{reservation.pk}/"
            if reservation
            else f"/admin/payments/paymentattempt/{attempt.pk}/change/"
        )
        valid = bool(reservation and reservation.normalized_status in Reservation.ACTIVE_STATUSES)
        if request:
            valid = valid and request.status == "paid"
        if modification:
            valid = valid and modification.status == "completed"
        state = (
            "الحجز مؤكد" if valid else "السداد موثق؛ تأكيد الحجز أو التعديل يحتاج مراجعة الإدارة"
        )
    return "\n".join(
        [
            f"✅ {BRAND} — استلام دفعة من الموقع",
            f"العملية: {kind}",
            f"الضيف: {clean(guest)}",
            f"الشقة: {clean(prop.name_ar or prop.name_en)}",
            f"فترة الطلب: {period}",
            f"المبلغ المستلم الآن: {attempt.amount:.2f} SAR",
            f"إجمالي الطلب: {total:.2f} {currency}",
            f"وقت توثيق السداد: {timezone.localtime(attempt.verified_at):%Y-%m-%d %H:%M}"
            " (Asia/Riyadh)",
            f"مرجع الحجز: {clean(reference)}",
            f"الحالة: {state}",
            "هذه دفعة موثقة من HyperPay؛ راجع صفحة الحجز للرصيد والحالة الحالية.",
            settings.SITE_BASE_URL.rstrip("/") + path,
        ]
    )


@shared_task(
    name="apps.payments.team_notices.deliver_team_notice", soft_time_limit=45, time_limit=60
)
def deliver_team_notice(notice_id):
    if not enabled() or not settings.ULTRAMSG_ENABLED:
        return {"status": "disabled"}
    with transaction.atomic():
        notice = (
            PaymentTeamNotice.objects.select_for_update(of=("self",))
            .select_related(
                "payment_attempt__booking_intent__property",
                "payment_attempt__payment_request__modification_request",
                "payment_attempt__modification_request",
                "morning_attempt__stay__property",
            )
            .get(pk=notice_id)
        )
        if notice.status != "queued":
            return {"status": notice.status}
        attempt = notice.morning_attempt if notice.morning_attempt_id else notice.payment_attempt
        if not verified(attempt):
            notice.status, notice.error_code = "skipped", "capture_no_longer_settled"
            notice.save(update_fields=["status", "error_code"])
            return {"status": "skipped"}
        try:
            body = message(notice)
        except Exception:
            notice.status, notice.error_code = "failed", "message_build_failed"
            notice.save(update_fields=["status", "error_code"])
            return {"status": "failed"}
        # Commit the send claim before the provider POST. A worker crash or a
        # timeout is uncertain, never permission to POST the same notice again.
        notice.status = "sending"
        notice.save(update_fields=["status"])
    try:
        with UltraMsgClient() as client:
            result = client.send_text(
                recipient=settings.MORNING07_WORK_GROUP_ID,
                body=body,
                reference_id=f"website-payment-team:{notice.pk}",
            )
        provider_id = str(result.get("id") or "")
        if not provider_id:
            raise ValueError("provider_reference_missing")
    except Exception:
        PaymentTeamNotice.objects.filter(pk=notice.pk).update(
            status="uncertain", error_code="provider_outcome_uncertain"
        )
        return {"status": "uncertain"}
    PaymentTeamNotice.objects.filter(pk=notice.pk).update(
        status="accepted", provider_message_id=provider_id[:255], sent_at=timezone.now()
    )
    return {"status": "accepted"}


def recover_notices():
    """Recover this outbox only; never discover or replay historical captures."""
    if not enabled():
        return
    for notice_id in (
        PaymentTeamNotice.objects.filter(
            status="queued", created_at__lte=timezone.now() - timedelta(seconds=30)
        )
        .order_by("created_at")
        .values_list("pk", flat=True)[:3]
    ):
        dispatch(notice_id)
