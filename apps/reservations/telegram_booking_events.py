"""Booking/refund evidence for manager copies; never performs a financial action."""

import logging
import uuid
from datetime import timedelta
from decimal import Decimal, InvalidOperation

from django.conf import settings
from django.core.exceptions import ObjectDoesNotExist, ValidationError
from django.db import DatabaseError, transaction
from django.utils import timezone
from django.views.decorators.debug import sensitive_variables

from . import telegram_website_workgroup as transport
from . import telegram_workgroup as group
from .models import BookingModificationRequest, HostawayModificationOperation, RefundObligation
from .telegram_authority import Denied
from .telegram_stay_models import TelegramWebsiteHandoff

logger = logging.getLogger(__name__)
LABELS = {
    "ar": ("الحجز", "الوحدة", "الوصول", "المغادرة", "الإجمالي", "مبلغ الاسترداد", "مرجع الاسترداد"),
    "en": (
        "Booking",
        "Apartment",
        "Check-in",
        "Check-out",
        "Total",
        "Refund amount",
        "Refund reference",
    ),
    "fr": (
        "Réservation",
        "Appartement",
        "Arrivée",
        "Départ",
        "Total",
        "Montant remboursé",
        "Référence du remboursement",
    ),
}
REFUND_COPY = {
    "ar": "تم توثيق تحويل مبلغ الاسترداد. قد يستغرق ظهوره في الحساب وقتًا وفق جهة الدفع.",
    "en": (
        "The refund transfer has been recorded. It may take time to appear, "
        "depending on the payment provider."
    ),
    "fr": (
        "Le transfert du remboursement a été enregistré. Son affichage peut prendre "
        "du temps selon le prestataire de paiement."
    ),
}
REFUND_HEADING = {
    "ar": "تأكيد الاسترداد",
    "en": "Refund confirmation",
    "fr": "Confirmation du remboursement",
}


@sensitive_variables()
def source(source_id):
    from apps.accounts.phone_messages import phone_logo
    from apps.notifications.services.email import (
        MESSAGES,
        SUBJECTS,
        _format_money,
        _localized_property_name,
    )

    kind, raw_id = source_id.split(":", 1)
    key = uuid.UUID(raw_id)
    if str(key) != raw_id:
        raise Denied
    if kind == "refund":
        event = RefundObligation.objects.select_related(
            "reservation__booking_intent", "reservation__property", "modification_request"
        ).get(pk=key)
        booking = event.reservation
        if (
            event.status != RefundObligation.Status.TRANSFERRED
            or event.amount <= 0
            or not event.transferred_at
            or not event.transfer_reference
        ):
            raise Denied
        detail = event.calculation.get("hyperpay_refund", {})
        if not isinstance(detail, dict):
            raise Denied
        if detail.get("state") == "confirmed":
            from apps.payments.models import PaymentAttempt

            if (
                not detail.get("original_payment_id")
                or detail.get("provider_refund_id", "")[:100] != event.transfer_reference
                or Decimal(str(detail.get("amount", "0"))) != event.amount
                or detail.get("currency") != event.currency
                or not PaymentAttempt.objects.filter(
                    booking_intent_id=booking.booking_intent_id,
                    provider_payment_id=detail.get("original_payment_id"),
                    verified_at__isnull=False,
                    status__in=[
                        PaymentAttempt.Status.PARTIALLY_REFUNDED,
                        PaymentAttempt.Status.REFUNDED,
                    ],
                    currency=event.currency,
                ).exists()
            ):
                raise Denied
        else:
            from apps.notifications.models import AuditLog

            attestation = (
                AuditLog.objects.filter(
                    action="refund.marked_transferred",
                    object_type="RefundObligation",
                    object_reference=event.public_reference,
                    actor_user_id=event.transferred_by_id,
                    created_at__gte=event.transferred_at - timedelta(seconds=5),
                )
                .order_by("-created_at")
                .values_list("metadata", flat=True)
                .first()
                if event.transferred_by_id
                else None
            )
            if (
                not attestation
                or attestation.get("currency") != event.currency
                or Decimal(str(attestation.get("amount", "0"))) != event.amount
            ):
                raise Denied
        created = event.transferred_at
        purpose, refund = "refund_confirmation", event
        basis = {
            "event": str(key),
            "transferred_at": created.isoformat(),
            "amount": str(event.amount),
            "currency": event.currency,
            "evidence": transport.digest({"reference": event.transfer_reference, "detail": detail}),
        }
    else:
        raise Denied
    modification = event.modification_request
    if modification:
        cancelled = (
            modification.request_type == BookingModificationRequest.RequestType.CANCEL_RESERVATION
        )
        if (
            modification.reservation_id != booking.pk
            or modification.status != BookingModificationRequest.Status.COMPLETED
            or not modification.completed_at
            or not modification.hostaway_operations.filter(
                status=HostawayModificationOperation.Status.SUCCEEDED
            ).exists()
            or (cancelled and booking.normalized_status != booking.Status.CANCELLED)
            or (
                not cancelled
                and (
                    booking.normalized_status not in booking.ACTIVE_STATUSES
                    or (
                        modification.new_check_in,
                        modification.new_check_out,
                        modification.new_guests,
                        modification.new_total,
                        modification.currency,
                    )
                    != (
                        booking.check_in,
                        booking.check_out,
                        booking.guests,
                        booking.total_price,
                        booking.currency,
                    )
                )
            )
        ):
            raise Denied
        basis.update(
            modification=str(modification.pk),
            modification_completed_at=modification.completed_at.isoformat(),
        )
    intent = booking.booking_intent
    if (
        not intent
        or settings.HYPERPAY_ENVIRONMENT != "production"
        or booking.is_test
        or not booking.hostaway_reservation_id
        or not transport.admitted(created)
        or created > timezone.now()
    ):
        raise Denied
    language = intent.language if intent.language in LABELS else "ar"
    labels = LABELS[language]
    name = f"{intent.guest_first_name} {intent.guest_last_name}".strip()
    property_name = _localized_property_name(booking.property, language) if booking.property else ""
    message_type = (
        "reservation_cancelled"
        if booking.normalized_status == booking.Status.CANCELLED
        else "reservation_modified"
        if modification
        else None
    )
    lines = [
        "Luxury Smart Apartments",
        SUBJECTS[message_type][language] if message_type else REFUND_HEADING[language],
        MESSAGES[message_type][language] if message_type else "",
        REFUND_COPY[language],
        "",
        f"{labels[0]}: {booking.public_reference}",
        f"{labels[1]}: {property_name}",
        f"{labels[2]}: {booking.check_in.isoformat()}",
        f"{labels[3]}: {booking.check_out.isoformat()}",
        f"{labels[4]}: {_format_money(booking.total_price, booking.currency, language)}",
    ]
    if refund:
        lines += [
            f"{labels[5]}: {_format_money(refund.amount, refund.currency, language)}",
            f"{labels[6]}: {refund.public_reference}",
        ]
    basis.update(
        booking=str(booking.pk),
        hostaway=str(booking.hostaway_reservation_id),
        check_in=booking.check_in.isoformat(),
        check_out=booking.check_out.isoformat(),
        total=str(booking.total_price),
        currency=booking.currency,
        status=booking.normalized_status,
    )
    from .telegram_refund_copy_requests import project

    created = project(refund, basis)
    return (
        "\n".join(lines),
        phone_logo(),
        language,
        intent.guest_phone,
        name,
        basis,
        created,
        purpose,
    )


def queue_event(kind, event_id):
    """Persist a new, future-only source job before scheduling; no historical scan."""
    if not group.enabled():
        return
    source_id = f"{kind}:{event_id}"
    try:
        content = transport.source_copy("booking_event", source_id)
        key = "site01wg:booking_event:" + transport.sha(source_id)
        row, created = TelegramWebsiteHandoff.objects.get_or_create(
            job_key=key,
            defaults={
                "source_kind": "booking_event",
                "source_id": source_id,
                "source_revision": content["source_revision"],
                "reference_id": "site01wg-" + transport.sha(key),
                "phone_hash": content["phone_hash"],
                "content_sha256": content["content_sha256"],
                "asset_sha256": content["asset_sha256"],
                "not_before": content["not_before"],
                "expires_at": content["expires_at"],
            },
        )
        if created:
            from .tasks import deliver_telegram_booking_event

            transaction.on_commit(
                lambda: deliver_telegram_booking_event.delay(str(row.pk)), robust=True
            )
    except (
        Denied,
        DatabaseError,
        ObjectDoesNotExist,
        ValidationError,
        ValueError,
        TypeError,
        KeyError,
        InvalidOperation,
    ):
        logger.warning(
            "Booking workgroup source held kind=%s",
            kind if kind in {"modification", "refund"} else "invalid",
        )


@sensitive_variables()
def dispatch(row_id):
    if not group.enabled():
        return "paused"
    with transaction.atomic():
        row = TelegramWebsiteHandoff.objects.select_for_update().get(
            pk=row_id, source_kind="booking_event"
        )
        if row.state != "planned":
            return row.state
        try:
            content = transport.source_copy(row.source_kind, row.source_id)
            if any(
                content[k] != getattr(row, k)
                for k in ("source_revision", "phone_hash", "content_sha256", "asset_sha256")
            ):
                raise Denied
            if not row.not_before <= timezone.now() < row.expires_at:
                raise Denied
        except (
            Denied,
            ObjectDoesNotExist,
            ValidationError,
            ValueError,
            TypeError,
            KeyError,
            InvalidOperation,
        ):
            row.state = "held"
            row.save(update_fields=["state", "updated_at"])
            return "held"
        row.state = "dispatching"
        row.save(update_fields=["state", "updated_at"])
    transport.call(row, "website_handoff")
    row.refresh_from_db()
    return row.state
