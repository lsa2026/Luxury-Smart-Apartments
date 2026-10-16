"""Consent-aware invitations and transactional point notices via the existing queue."""

from datetime import datetime, timedelta

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db.models import CharField, Exists, OuterRef
from django.db.models.functions import Cast
from django.utils import timezone
from django.utils.translation import override

from apps.notifications.services.email import EmailProviderError, queue_email
from apps.reservations.models import Reservation

from .models import Ledger, Member
from .services import enabled, issue_invite, launch_at

SUBJECTS = {
    "loyalty_join_invitation": {
        "ar": "دعوة إلى برنامج مكافآت ضيوفنا",
        "en": "An invitation to our guest rewards programme",
        "fr": "Une invitation à notre programme de fidélité",
    },
    "loyalty_movement": {
        "ar": "حركة جديدة في نقاط مكافآتك",
        "en": "An update to your rewards points",
        "fr": "Une nouvelle opération sur vos points",
    },
    "loyalty_invite_ready": {
        "ar": "دعوة أصدقائك أصبحت متاحة",
        "en": "You can now invite your friends",
        "fr": "Vous pouvez désormais inviter vos amis",
    },
}
MESSAGES = {
    "loyalty_join_invitation": {
        "ar": (
            "سعداء بإقامتك معنا. اطلع على برنامج مكافآتنا وشروطه من حسابك. "
            "الانضمام مجاني واختياري، ولا تُفعّل العضوية إلا بموافقتك الصريحة."
        ),
        "en": (
            "We are delighted to host you. "
            "View our rewards programme and its terms in your account. "
            "Joining is free and optional, and requires your explicit consent."
        ),
        "fr": (
            "Nous sommes ravis de vous accueillir. Découvrez le programme et ses conditions. "
            "L’adhésion est gratuite, facultative et soumise à votre accord explicite."
        ),
    },
    "loyalty_movement": {
        "ar": (
            "سُجلت حركة جديدة في حساب نقاطك. راجع رقم العملية والنقاط المضافة أو المستخدمة "
            "ورصيدك في كشف الحساب."
        ),
        "en": (
            "A new points transaction has been recorded. View its reference, credits or debits, "
            "and your balance in your statement."
        ),
        "fr": (
            "Une nouvelle opération a été enregistrée. Consultez sa référence, les points crédités "
            "ou utilisés et votre solde dans votre relevé."
        ),
    },
    "loyalty_invite_ready": {
        "ar": (
            "شكرًا لإتمام إقامتك معنا. رابط دعوة أصدقائك متاح في حسابك؛ "
            "راجع شروط البرنامج قبل مشاركته."
        ),
        "en": (
            "Thank you for completing your stay. "
            "Your invitation link is available in your account. "
            "Review the programme terms before sharing it."
        ),
        "fr": (
            "Merci pour votre séjour. Votre lien d’invitation est disponible dans votre compte. "
            "Consultez les conditions avant de le partager."
        ),
    },
}


def messages_enabled():
    from .launch import pilot_only
    from .views import approved_terms

    # An acceptance run never launches an automatic guest mailing campaign.
    return enabled() and settings.LOYALTY_MESSAGES_ENABLED and approved_terms() and not pilot_only()


def queue_movement(ledger_id):
    if not messages_enabled():
        return
    row = Ledger.objects.select_related("member__customer").get(pk=ledger_id)
    if row.member.status != "active" or not row.member.customer.is_active:
        return
    return queue_email(
        message_type="loyalty_movement",
        recipient=row.member.customer.email,
        recipient_source="loyalty_ledger",
        recipient_reference=str(row.pk),
        language=row.member.language,
        idempotency_key=f"loyalty-movement:{row.pk}",
    )


def dispatch_due(limit=100):
    if not messages_enabled() or not settings.EMAIL_DELIVERY_ENABLED:
        return {"status": "disabled", "queued": 0}
    from apps.notifications.models import EmailDelivery

    maximum = max(1, min(limit, 500))
    queued = 0
    # Recover queue failures after a committed movement, without re-posting it.
    pending = Ledger.objects.filter(member__status="active", member__customer__is_active=True)
    pending = (
        pending.annotate(notification_reference=Cast("pk", CharField()))
        .filter(
            ~Exists(
                EmailDelivery.objects.filter(
                    recipient_source="loyalty_ledger",
                    recipient_reference=OuterRef("notification_reference"),
                )
            )
        )
        .order_by("pk")
    )
    for row in pending.iterator(chunk_size=100):
        if queue_movement(row.pk):
            queued += 1
        if queued >= maximum:
            return {"status": "completed", "queued": queued}
    bookings = (
        Reservation.objects.select_related("booking_intent__customer", "property")
        .filter(
            source_type=Reservation.SourceType.DIRECT_WEBSITE,
            created_at__gte=launch_at(),
            normalized_status__in=Reservation.ACTIVE_STATUSES,
            check_in__lte=timezone.now().date(),
            check_out__gte=timezone.now().date(),
        )
        .order_by("pk")
    )
    for booking in bookings.iterator(chunk_size=100):
        arrival = booking.loyalty_state.get("arrival")
        intent = booking.booking_intent
        if not arrival or not intent or not intent.marketing_consent or not intent.guest_email:
            continue
        at = datetime.fromisoformat(arrival["at"])
        due = at + timedelta(hours=1)
        if timezone.is_naive(at) or due > timezone.now():
            continue
        if intent.customer_id and Member.objects.filter(customer_id=intent.customer_id).exists():
            continue
        key = f"loyalty-join:{booking.pk}"
        if EmailDelivery.objects.filter(idempotency_key=key).exists():
            continue
        queue_email(
            message_type="loyalty_join_invitation",
            recipient=intent.guest_email,
            recipient_source="loyalty_invitation",
            recipient_reference=booking.public_reference,
            language=intent.language,
            idempotency_key=key,
        )
        queued += 1
        if queued >= max(1, min(limit, 500)):
            break
    # Completion is verified by the same receipt/arrival checks as the engine.
    for member in (
        Member.objects.select_related("customer")
        .filter(status="active", customer__is_active=True)
        .order_by("pk")
        .iterator(chunk_size=100)
    ):
        key = f"loyalty-ready:{member.pk}"
        if EmailDelivery.objects.filter(idempotency_key=key).exists():
            continue
        if member.invitation_token:
            queue_email(
                message_type="loyalty_invite_ready",
                recipient=member.customer.email,
                recipient_source="loyalty_member",
                recipient_reference=str(member.pk),
                language=member.language,
                idempotency_key=key,
            )
            queued += 1
            if queued >= maximum:
                break
            continue
        for booking in (
            Reservation.objects.select_related("property", "booking_intent__quote")
            .filter(
                booking_intent__customer=member.customer,
                created_at__gte=launch_at(),
                normalized_status__in=Reservation.ACTIVE_STATUSES,
                check_out__lte=timezone.now().date(),
            )
            .order_by("check_out", "pk")
        ):
            try:
                issue_invite(member.customer, booking.pk)
            except ValidationError:
                continue
            queue_email(
                message_type="loyalty_invite_ready",
                recipient=member.customer.email,
                recipient_source="loyalty_member",
                recipient_reference=str(member.pk),
                language=member.language,
                idempotency_key=f"loyalty-ready:{member.pk}",
            )
            queued += 1
            break
        if queued >= max(1, min(limit, 500)):
            break
    return {"status": "completed", "queued": queued}


def resolve_recipient(delivery):
    """Recheck state at send time; never send stale invitations after cancellation."""
    if not messages_enabled():
        raise EmailProviderError("loyalty_messages_disabled", permanent=True)
    base = settings.SITE_BASE_URL.rstrip("/")
    if delivery.recipient_source == "loyalty_invitation":
        booking = Reservation.objects.select_related("booking_intent", "property").get(
            public_reference=delivery.recipient_reference
        )
        intent = booking.booking_intent
        arrival = booking.loyalty_state.get("arrival", {})
        if (
            not intent
            or not intent.marketing_consent
            or booking.normalized_status not in Reservation.ACTIVE_STATUSES
            or not arrival
        ):
            raise EmailProviderError("loyalty_invitation_not_eligible", permanent=True)
        from .site_adapter import stay_times

        if (
            timezone.now() >= stay_times(booking)[1]
            or datetime.fromisoformat(arrival["at"]) + timedelta(hours=1) > timezone.now()
        ):
            raise EmailProviderError("loyalty_invitation_outside_stay", permanent=True)
        if intent.customer_id and Member.objects.filter(customer_id=intent.customer_id).exists():
            raise EmailProviderError("loyalty_already_joined", permanent=True)
        return intent.guest_email, {
            "first_name": intent.guest_first_name,
            "action_url": f"{base}/my-bookings/rewards/",
        }
    if delivery.recipient_source == "loyalty_ledger":
        row = Ledger.objects.select_related("member__customer").get(pk=delivery.recipient_reference)
        from .statement import decorate

        with override(delivery.language):
            decorate([row])
        member = row.member
        values = {
            "operation": row.operation_number,
            "credit": str(row.credit),
            "debit": str(row.debit),
            "balance": str(row.member_balance_after),
            "transaction_kind": row.ui_kind,
            "counterpart": row.ui_counterpart,
            "balance_before": str(row.ui_balance_before),
            "gift_outgoing": bool(row.debit),
        }
    else:
        member = Member.objects.select_related("customer").get(pk=delivery.recipient_reference)
        if not member.invitation_token:
            raise EmailProviderError("loyalty_invitation_not_ready", permanent=True)
        values = {}
    if member.status != "active" or not member.customer.is_active:
        raise EmailProviderError("loyalty_membership_inactive", permanent=True)
    return member.customer.email, {
        **values,
        "first_name": member.customer.first_name,
        "action_url": f"{base}/my-bookings/rewards/",
    }
