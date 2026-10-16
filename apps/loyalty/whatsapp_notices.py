"""Post-commit transactional notices. No history scan, marketing or send replay."""

import logging
from datetime import timedelta
from urllib.parse import urlparse

from celery import shared_task
from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone
from django.utils.crypto import salted_hmac
from django.utils.translation import override

from apps.accounts.models import CustomerProfile, VerifiedPhone
from apps.accounts.phone_auth import _provider_authenticated
from apps.accounts.phone_messages import phone_logo, supported_language
from apps.core.templatetags.presentation import localized_datetime, localized_number
from apps.notifications.services.ultramsg import UltraMsgClient

from .launch import participant_allowed
from .models import Member, MovementNotice
from .statement import decorate

logger = logging.getLogger(__name__)
STATUSES = {
    "queued": "بانتظار الإرسال",
    "sending": "جارٍ طلب الإرسال",
    "accepted": "قبله مزود واتساب؛ الوصول غير مؤكد",
    "uncertain": "نتيجة الإرسال غير مؤكدة؛ لا إعادة تلقائية",
    "failed": "تعذر تجهيز الإشعار",
    "skipped": "لم يُرسل لعدم تحقق شروط الإرسال",
}


def enabled():
    return bool(settings.LOYALTY_ENABLED and settings.LOYALTY_WHATSAPP_NOTICES_ENABLED)


def phone_digest(phone):
    return salted_hmac("loyalty-movement-mobile-v1", phone, algorithm="sha256").hexdigest()


def _name(value):
    # Never let a guest-controlled name inject formatting, URLs or phone numbers.
    if "@" in value:
        return ""
    return " ".join("".join(c for c in value if c.isalpha() or c.isspace()).split())[:60]


def movement_caption(row, language):
    language = supported_language(language) or "ar"
    base = settings.SITE_BASE_URL.rstrip("/")
    if urlparse(base).scheme != "https" or not urlparse(base).netloc:
        raise ValidationError("Invalid statement destination.")
    with override(language):
        decorate([row])
        credit, debit, balance = [
            localized_number(format(value, ".2f"))
            for value in (row.credit, row.debit, row.member_balance_after)
        ]
        (
            credit_label,
            debit_label,
            balance_label,
            reference,
            action,
            gift_to,
            gift_from,
            member,
            rate_label,
        ) = {
            "ar": (
                "النقاط المضافة",
                "النقاط المخصومة",
                "رصيدك بعد العملية",
                "رقم العملية",
                "كشف نقاطك",
                "أهديت إلى",
                "هدية من",
                "عضو",
                "نسبة المكافأة",
            ),
            "en": (
                "Points added",
                "Points deducted",
                "Your balance after this transaction",
                "Transaction",
                "Your statement",
                "Gifted to",
                "Gift from",
                "Member",
                "Reward rate",
            ),
            "fr": (
                "Points ajoutés",
                "Points débités",
                "Votre solde après l’opération",
                "Opération",
                "Votre relevé",
                "Offerts à",
                "Cadeau de",
                "Membre",
                "Taux de récompense",
            ),
        }[language]
        lines = ["✨ *Luxury Living*", f"*{row.ui_kind}*"]
        if row.ui_counterpart:
            party = row.ui_counterpart
            name = _name(party["name"]) or member
            lines.append(f"{gift_to if row.debit else gift_from}: {name} · {party['number']}")
        lines.append(
            f"{credit_label if row.credit else debit_label}: *{credit if row.credit else debit}*"
        )
        if row.event_type == "earn" and row.ui_rate is not None:
            rate = format(row.ui_rate, ".6f").rstrip("0").rstrip(".")
            lines.append(
                f"{rate_label}: {localized_number(rate)}٪"
                if language == "ar"
                else f"{rate_label}: {rate}%"
            )
        lines.extend(
            [
                f"{balance_label}: *{balance}*",
                localized_datetime(row.occurred_at),
                f"{reference}: {row.operation_number}",
                f"{action}:\n{base}/my-bookings/rewards/",
            ]
        )
        caption = "\n\n".join(lines)
        if len(caption.encode("utf-16-le")) // 2 > 1024:
            raise ValidationError("Movement caption exceeds the provider limit.")
        return caption


def _dispatch(key):
    try:
        deliver_movement_notice.delay(str(key))
    except Exception:
        # Durable queued rows are recovered by the scheduler. No sensitive error body.
        logger.warning("loyalty_notice_dispatch_deferred")


def queue_notice(row):
    """Called only for a newly posted ledger row, within its transaction."""
    if not enabled():
        return None
    profile = CustomerProfile.objects.filter(user_id=row.customer_id).first()
    binding = VerifiedPhone.objects.filter(user_id=row.customer_id).first()
    language = (
        supported_language(profile.preferred_language if profile else "") or row.member.language
    )
    eligible = bool(binding and profile and profile.is_email_verified)
    notice, created = MovementNotice.objects.get_or_create(
        ledger=row,
        defaults={
            "created_at": timezone.now(),
            "next_attempt_at": timezone.now(),
            "recipient_binding": binding,
            "binding_version": binding.version if binding else None,
            "phone_digest": phone_digest(binding.phone) if binding else "",
            "language": supported_language(language) or "ar",
            "status": "queued" if eligible else "skipped",
            "error_code": "" if eligible else "verified_mobile_required",
        },
    )
    if created and eligible:
        transaction.on_commit(lambda: _dispatch(notice.pk), robust=True)
    return notice


def _provider_ready():
    return bool(
        settings.ULTRAMSG_ENABLED and settings.ULTRAMSG_INSTANCE_ID and settings.ULTRAMSG_TOKEN
    )


def _update(key, **values):
    MovementNotice.objects.filter(pk=key, status="sending").update(**values)


@shared_task(ignore_result=True, soft_time_limit=35, time_limit=45)
def deliver_movement_notice(key):
    if not enabled():
        return
    initial = MovementNotice.objects.select_related("ledger").filter(pk=key).first()
    if initial is None:
        return
    with transaction.atomic():
        # Serialize claim/order per member, without holding a database lock during HTTP.
        Member.objects.select_for_update().get(pk=initial.ledger.member_id)
        notice = (
            MovementNotice.objects.select_for_update(of=("self",))
            .select_related("ledger__member__customer", "recipient_binding")
            .get(pk=key)
        )
        if notice.status != "queued" or notice.next_attempt_at > timezone.now():
            return
        row = notice.ledger
        binding, customer = notice.recipient_binding, row.member.customer
        profile = CustomerProfile.objects.filter(user=customer).first()
        if (
            not customer.is_active
            or not participant_allowed(customer)
            or not profile
            or not profile.is_email_verified
            or not binding
            or binding.user_id != row.customer_id
            or binding.version != notice.binding_version
            or phone_digest(binding.phone) != notice.phone_digest
        ):
            notice.status, notice.error_code = "skipped", "recipient_changed"
            notice.save(update_fields=["status", "error_code"])
            return
        if MovementNotice.objects.filter(
            ledger__member_id=row.member_id,
            ledger__sequence__lt=row.sequence,
            status__in=["queued", "sending"],
        ).exists():
            return
        if not _provider_ready():
            return  # Do not block financial postings or lose queued notifications.
        try:
            if notice.template_version != "movement-whatsapp-v1":
                raise ValidationError("Unknown movement design.")
            caption, content = movement_caption(row, notice.language), phone_logo()
        except Exception:
            notice.status, notice.error_code = "failed", "design_unavailable"
            notice.save(update_fields=["status", "error_code"])
            return
        notice.status, notice.started_at = "sending", timezone.now()
        notice.attempts += 1
        notice.save(update_fields=["status", "started_at", "attempts"])
        phone = binding.phone
    phase = "connection"
    try:
        with UltraMsgClient() as client:
            result = client.http.get(
                f"/{settings.ULTRAMSG_INSTANCE_ID}/instance/status",
                params={"token": settings.ULTRAMSG_TOKEN},
            )
            if result.status_code != 200 or not _provider_authenticated(result.json()):
                raise RuntimeError("Provider not ready.")
            # Recheck ownership and the kill switch immediately before submission.
            if (
                not enabled()
                or not VerifiedPhone.objects.filter(
                    pk=binding.pk,
                    user_id=row.customer_id,
                    version=notice.binding_version,
                    phone=phone,
                ).exists()
            ):
                _update(key, status="skipped", error_code="recipient_changed")
                return
            phase = "send"
            response = client.send_image(
                recipient=phone,
                content=content,
                caption=caption,
                reference_id=f"lsa-rewards-{notice.pk.hex}",
            )
    except Exception:
        if phase == "send":
            # Provider may already have accepted it. No blind resend or text fallback.
            _update(key, status="uncertain", error_code="outcome_unknown")
        else:
            _update(
                key,
                status="queued",
                error_code="provider_unavailable",
                next_attempt_at=timezone.now()
                + timedelta(seconds=min(60 * 2 ** min(notice.attempts, 5), 1800)),
            )
        return
    reference = str(response.get("id", "")) if isinstance(response, dict) else ""
    reference = "".join(c for c in reference if c.isalnum() or c in "-_")[:80]
    _update(
        key,
        status="accepted",
        error_code="",
        accepted_at=timezone.now(),
        provider_reference=reference,
    )


def recover_notices(limit=100):
    if not enabled():
        return {"status": "disabled", "queued": 0}
    # Only delivery records created at posting time. Never synthesize from old Ledger rows.
    MovementNotice.objects.filter(
        status="sending", started_at__lte=timezone.now() - timedelta(minutes=10)
    ).update(status="uncertain", error_code="worker_outcome_unknown")
    queued = 0
    for key in (
        MovementNotice.objects.filter(status="queued", next_attempt_at__lte=timezone.now())
        .order_by("ledger__sequence")
        .values_list("pk", flat=True)[: min(max(limit, 1), 500)]
    ):
        _dispatch(key)
        queued += 1
    return {"status": "completed", "queued": queued}
