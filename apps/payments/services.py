"""Local cardless payment simulation used only behind the development guard."""

from dataclasses import dataclass

from django.db import transaction
from django.utils import timezone
from django.utils.crypto import salted_hmac

from apps.reservations.models import BookingIntent, BookingModificationRequest, Reservation

from .models import PaymentAttempt

SANDBOX_PROVIDER = "local_sandbox"


@dataclass(frozen=True, slots=True)
class SandboxResult:
    attempt: PaymentAttempt
    reservation: Reservation | None = None


def _key(kind: str, reference: str, action: str) -> str:
    return salted_hmac("payment-sandbox", f"{kind}:{reference}:{action}").hexdigest()


def _attempt(
    *,
    intent: BookingIntent,
    modification: BookingModificationRequest | None,
    amount,
    currency: str,
    kind: str,
    reference: str,
    action: str,
) -> PaymentAttempt:
    status = {
        "success": PaymentAttempt.Status.SUCCEEDED,
        "failure": PaymentAttempt.Status.FAILED,
        "cancel": PaymentAttempt.Status.CANCELLED,
    }[action]
    attempt, _ = PaymentAttempt.objects.get_or_create(
        idempotency_key=_key(kind, reference, action),
        defaults={
            "booking_intent": intent,
            "modification_request": modification,
            "provider": SANDBOX_PROVIDER,
            "provider_reference": f"sandbox-{reference[:12]}-{action}",
            "amount": amount,
            "currency": currency,
            "status": status,
            "failure_code": "sandbox_declined" if action == "failure" else "",
        },
    )
    return attempt


@transaction.atomic
def simulate_booking(intent: BookingIntent, action: str) -> SandboxResult:
    intent = BookingIntent.objects.select_for_update().select_related("property").get(pk=intent.pk)
    attempt = _attempt(
        intent=intent,
        modification=None,
        amount=intent.total_price,
        currency=intent.currency,
        kind="booking",
        reference=intent.public_reference,
        action=action,
    )
    if action != "success":
        return SandboxResult(attempt=attempt)

    now = timezone.now()
    reservation, created = Reservation.objects.select_for_update().get_or_create(
        booking_intent=intent,
        defaults={
            "property": intent.property,
            "hostaway_listing_id": intent.property.hostaway_listing_id,
            "source_type": Reservation.SourceType.DIRECT_WEBSITE,
            "normalized_status": Reservation.Status.CONFIRMED,
            "payment_status": "sandbox_paid",
            "is_test": True,
            "check_in": intent.check_in,
            "check_out": intent.check_out,
            "nights": intent.nights,
            "guests": intent.guests,
            "currency": intent.currency,
            "total_price": intent.total_price,
            "confirmed_at": now,
        },
    )
    if not created and not reservation.is_test:
        raise ValueError("Sandbox payments cannot alter a non-test reservation.")
    if reservation.normalized_status != Reservation.Status.CONFIRMED:
        reservation.source_type = Reservation.SourceType.DIRECT_WEBSITE
        reservation.normalized_status = Reservation.Status.CONFIRMED
        reservation.payment_status = "sandbox_paid"
        reservation.is_test = True
        reservation.confirmed_at = now
        reservation.cancelled_at = None
        reservation.save(
            update_fields=[
                "source_type",
                "normalized_status",
                "payment_status",
                "is_test",
                "confirmed_at",
                "cancelled_at",
                "updated_at",
            ]
        )
    if intent.status != BookingIntent.Status.COMPLETED:
        intent.status = BookingIntent.Status.COMPLETED
        intent.save(update_fields=["status", "updated_at"])
    return SandboxResult(attempt=attempt, reservation=reservation)


@transaction.atomic
def simulate_modification(
    modification: BookingModificationRequest,
    action: str,
) -> SandboxResult:
    modification = (
        BookingModificationRequest.objects.select_for_update()
        .get(pk=modification.pk)
    )
    intent = modification.reservation.booking_intent
    if intent is None:
        raise ValueError("A sandbox modification requires a local booking intent.")
    attempt = _attempt(
        intent=intent,
        modification=modification,
        amount=modification.price_difference,
        currency=modification.currency,
        kind="modification",
        reference=modification.public_reference,
        action=action,
    )
    if (
        action == "success"
        and modification.status == BookingModificationRequest.Status.AWAITING_PAYMENT
    ):
        modification.status = BookingModificationRequest.Status.PENDING_ADMIN_APPROVAL
        modification.save(update_fields=["status", "updated_at"])
    return SandboxResult(attempt=attempt)
