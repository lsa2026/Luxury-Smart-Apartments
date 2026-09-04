"""Future Hostaway booking orchestration, disabled by default and payment-gated."""

import logging
import secrets
from dataclasses import dataclass
from time import monotonic
from typing import Any

from django.conf import settings
from django.db import transaction
from django.utils import timezone
from django.utils.crypto import salted_hmac

from apps.integrations.hostaway.client import HostawayClient
from apps.integrations.hostaway.exceptions import (
    HostawayAuthenticationError,
    HostawayAvailabilityError,
    HostawayConfigurationError,
    HostawayNetworkError,
    HostawayRateLimitError,
    HostawayResponseError,
    HostawayServerError,
)
from apps.payments.models import PaymentAttempt
from apps.reservations.models import (
    BookingIntent,
    HostawayReservationOperation,
    Reservation,
)
from apps.reservations.services.availability import (
    AvailabilityRequest,
    AvailabilityResult,
    AvailabilityService,
)

from .reservation_payloads import build_hostaway_reservation_request

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ReservationCreationOutcome:
    code: str
    reservation: Reservation
    operation: HostawayReservationOperation | None = None


def prepare_local_reservation(booking_intent: BookingIntent) -> Reservation:
    """Idempotently create local state without contacting Hostaway."""
    if booking_intent.status != BookingIntent.Status.AWAITING_PAYMENT:
        raise ValueError("booking_intent_not_awaiting_payment")
    with transaction.atomic():
        locked_intent = (
            BookingIntent.objects.select_for_update()
            .select_related("property")
            .get(pk=booking_intent.pk)
        )
        existing = Reservation.objects.filter(booking_intent=locked_intent).first()
        if existing:
            return existing
        reservation = Reservation(
            booking_intent=locked_intent,
            property=locked_intent.property,
            hostaway_listing_id=locked_intent.property.hostaway_listing_id,
            hostaway_listing_map_id=locked_intent.property.hostaway_listing_map_id,
            source_type=Reservation.SourceType.DIRECT_WEBSITE,
            normalized_status=Reservation.Status.AWAITING_PAYMENT,
            check_in=locked_intent.check_in,
            check_out=locked_intent.check_out,
            nights=locked_intent.nights,
            guests=locked_intent.guests,
            currency=locked_intent.currency,
            total_price=locked_intent.total_price,
        )
        reservation.full_clean()
        reservation.save(force_insert=True)
        return reservation


class HostawayBookingService:
    """Coordinates one non-retryable Hostaway create call after all safeguards."""

    def __init__(
        self,
        *,
        client: HostawayClient | None = None,
        availability_service: AvailabilityService | None = None,
    ) -> None:
        self.client = client or HostawayClient(
            timeout=settings.HOSTAWAY_RESERVATION_REQUEST_TIMEOUT_SECONDS
        )
        self.availability_service = availability_service or AvailabilityService()
        self._owns_client = client is None
        self._owns_availability = availability_service is None

    def close(self) -> None:
        if self._owns_client:
            self.client.close()
        if self._owns_availability:
            self.availability_service.close()

    def __enter__(self) -> "HostawayBookingService":
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    def create_hostaway_reservation(
        self,
        reservation: Reservation,
    ) -> ReservationCreationOutcome:
        if not settings.HOSTAWAY_LIVE_BOOKING_ENABLED:
            return self._block(reservation, "hostaway_live_booking_disabled")
        intent = reservation.booking_intent
        if intent is None:
            return self._block(reservation, "booking_intent_missing")
        payment = (
            PaymentAttempt.objects.filter(
                booking_intent=intent,
                status=PaymentAttempt.Status.SUCCEEDED,
                amount=reservation.total_price,
                currency=reservation.currency,
            )
            .order_by("-created_at")
            .first()
        )
        if payment is None:
            return self._block(reservation, "successful_payment_required")
        if payment.provider == "hyperpay" and settings.HYPERPAY_ENVIRONMENT == "test":
            return self._block(reservation, "test_payment_live_write_blocked")
        if reservation.property is None or reservation.property.hostaway_listing_map_id is None:
            return self._block(reservation, "listing_map_id_not_verified")
        if settings.HOSTAWAY_DIRECT_CHANNEL_ID is None:
            return self._block(reservation, "direct_channel_id_not_configured")
        if reservation.hostaway_reservation_id is not None:
            return ReservationCreationOutcome("already_confirmed", reservation)

        availability = self.availability_service.check(
            AvailabilityRequest(
                property=reservation.property,
                check_in=reservation.check_in,
                check_out=reservation.check_out,
                guests=reservation.guests,
            ),
            bypass_cache=True,
        )
        if not availability.is_available or availability.quote is None:
            return self._fail_without_post(reservation, "availability_lost")
        try:
            request = build_hostaway_reservation_request(
                reservation,
                current_quote=availability.quote,
            )
        except ValueError as exc:
            return self._fail_without_post(reservation, str(exc))

        operation, can_send = self._prepare_operation(reservation, availability)
        if not can_send:
            return ReservationCreationOutcome(
                operation.error_code or operation.status,
                reservation,
                operation,
            )

        request_started = monotonic()
        try:
            result = self.client.create_reservation_with_price_details(request)
        except (HostawayNetworkError, HostawayServerError) as exc:
            logger.warning(
                "Hostaway reservation create became uncertain: "
                "operation_id=%s reservation_ref=%s code=%s duration_ms=%s",
                operation.pk,
                reservation.public_reference[:8],
                type(exc).__name__,
                round((monotonic() - request_started) * 1000),
            )
            return self._finish_unknown(reservation, operation, "hostaway_create_uncertain")
        except (
            HostawayAuthenticationError,
            HostawayAvailabilityError,
            HostawayConfigurationError,
            HostawayRateLimitError,
            HostawayResponseError,
        ) as exc:
            logger.warning(
                "Hostaway reservation create failed: "
                "operation_id=%s reservation_ref=%s code=%s duration_ms=%s",
                operation.pk,
                reservation.public_reference[:8],
                type(exc).__name__,
                round((monotonic() - request_started) * 1000),
            )
            return self._finish_failed(reservation, operation, "hostaway_create_rejected")

        snapshot = result.snapshot
        if (
            snapshot.listing_map_id != request.listing_map_id
            or snapshot.check_in != reservation.check_in
            or snapshot.check_out != reservation.check_out
            or snapshot.currency != reservation.currency
            or snapshot.total_price != reservation.total_price
        ):
            return self._finish_unknown(
                reservation,
                operation,
                "hostaway_create_response_mismatch",
                hostaway_reservation_id=snapshot.reservation_id,
            )
        with transaction.atomic():
            locked_reservation = Reservation.objects.select_for_update().get(pk=reservation.pk)
            locked_operation = HostawayReservationOperation.objects.select_for_update().get(
                pk=operation.pk
            )
            now = timezone.now()
            locked_reservation.hostaway_reservation_id = snapshot.reservation_id
            locked_reservation.hostaway_listing_map_id = snapshot.listing_map_id
            locked_reservation.channel_id = snapshot.channel_id or request.channel_id
            locked_reservation.hostaway_status = snapshot.status
            locked_reservation.payment_status = snapshot.payment_status
            locked_reservation.normalized_status = Reservation.Status.CONFIRMED
            locked_reservation.confirmed_at = now
            locked_reservation.source_updated_at = snapshot.updated_at
            locked_reservation.last_synced_at = now
            locked_reservation.full_clean()
            locked_reservation.save()
            locked_operation.status = HostawayReservationOperation.Status.SUCCEEDED
            locked_operation.hostaway_reservation_id = snapshot.reservation_id
            locked_operation.completed_at = now
            locked_operation.error_code = ""
            locked_operation.save()
            BookingIntent.objects.filter(pk=intent.pk).update(
                status=BookingIntent.Status.COMPLETED,
                updated_at=now,
            )
            from apps.notifications.services.events import handle_reservation_confirmed

            transaction.on_commit(
                lambda: handle_reservation_confirmed(locked_reservation.pk)
            )
        reservation.refresh_from_db()
        operation.refresh_from_db()
        logger.info(
            "Hostaway reservation create succeeded: "
            "operation_id=%s reservation_ref=%s duration_ms=%s",
            operation.pk,
            reservation.public_reference[:8],
            round((monotonic() - request_started) * 1000),
        )
        return ReservationCreationOutcome("confirmed", reservation, operation)

    @staticmethod
    def _block(reservation: Reservation, code: str) -> ReservationCreationOutcome:
        operation = _ensure_blocked_operation(reservation, code)
        return ReservationCreationOutcome(code, reservation, operation)

    @staticmethod
    def _fail_without_post(
        reservation: Reservation,
        code: str,
    ) -> ReservationCreationOutcome:
        with transaction.atomic():
            locked = Reservation.objects.select_for_update().get(pk=reservation.pk)
            locked.normalized_status = Reservation.Status.CREATE_FAILED
            locked.save(update_fields=["normalized_status", "updated_at"])
        reservation.refresh_from_db()
        operation = _ensure_blocked_operation(reservation, code)
        return ReservationCreationOutcome(code, reservation, operation)

    @staticmethod
    def _prepare_operation(
        reservation: Reservation,
        availability: AvailabilityResult,
    ) -> tuple[HostawayReservationOperation, bool]:
        assert availability.quote is not None
        fingerprint = _request_fingerprint(reservation, availability)
        with transaction.atomic():
            locked = Reservation.objects.select_for_update().get(pk=reservation.pk)
            operation, created = (
                HostawayReservationOperation.objects.select_for_update().get_or_create(
                    reservation=locked,
                    operation_type=(HostawayReservationOperation.OperationType.CREATE_RESERVATION),
                    defaults={
                        "idempotency_key": secrets.token_urlsafe(32),
                        "request_fingerprint": fingerprint,
                        "status": HostawayReservationOperation.Status.PREPARED,
                    },
                )
            )
            if not created and operation.status in {
                HostawayReservationOperation.Status.SUCCEEDED,
                HostawayReservationOperation.Status.IN_PROGRESS,
                HostawayReservationOperation.Status.UNKNOWN,
            }:
                return operation, False
            if not created and operation.attempt_count > 0:
                return operation, False
            operation.status = HostawayReservationOperation.Status.IN_PROGRESS
            operation.attempt_count += 1
            operation.started_at = timezone.now()
            operation.request_fingerprint = fingerprint
            operation.error_code = ""
            operation.save()
            locked.normalized_status = Reservation.Status.CREATING
            locked.save(update_fields=["normalized_status", "updated_at"])
            return operation, True

    @staticmethod
    def _finish_unknown(
        reservation: Reservation,
        operation: HostawayReservationOperation,
        code: str,
        *,
        hostaway_reservation_id: int | None = None,
    ) -> ReservationCreationOutcome:
        return _finish_operation(
            reservation,
            operation,
            code=code,
            operation_status=HostawayReservationOperation.Status.UNKNOWN,
            reservation_status=Reservation.Status.CREATE_UNKNOWN,
            hostaway_reservation_id=hostaway_reservation_id,
        )

    @staticmethod
    def _finish_failed(
        reservation: Reservation,
        operation: HostawayReservationOperation,
        code: str,
    ) -> ReservationCreationOutcome:
        return _finish_operation(
            reservation,
            operation,
            code=code,
            operation_status=HostawayReservationOperation.Status.FAILED,
            reservation_status=Reservation.Status.CREATE_FAILED,
        )


def _ensure_blocked_operation(
    reservation: Reservation,
    code: str,
) -> HostawayReservationOperation:
    with transaction.atomic():
        operation, _ = HostawayReservationOperation.objects.get_or_create(
            reservation=reservation,
            operation_type=HostawayReservationOperation.OperationType.CREATE_RESERVATION,
            defaults={
                "idempotency_key": secrets.token_urlsafe(32),
                "request_fingerprint": _reservation_fingerprint(reservation),
                "status": HostawayReservationOperation.Status.BLOCKED,
                "error_code": code,
            },
        )
        if operation.status in {
            HostawayReservationOperation.Status.PREPARED,
            HostawayReservationOperation.Status.BLOCKED,
        } and operation.attempt_count == 0:
            operation.status = HostawayReservationOperation.Status.BLOCKED
            operation.error_code = code
            operation.save(update_fields=["status", "error_code", "updated_at"])
        return operation


def _finish_operation(
    reservation: Reservation,
    operation: HostawayReservationOperation,
    *,
    code: str,
    operation_status: str,
    reservation_status: str,
    hostaway_reservation_id: int | None = None,
) -> ReservationCreationOutcome:
    with transaction.atomic():
        locked_reservation = Reservation.objects.select_for_update().get(pk=reservation.pk)
        locked_operation = HostawayReservationOperation.objects.select_for_update().get(
            pk=operation.pk
        )
        now = timezone.now()
        locked_reservation.normalized_status = reservation_status
        if hostaway_reservation_id:
            locked_reservation.hostaway_reservation_id = hostaway_reservation_id
        locked_reservation.save()
        locked_operation.status = operation_status
        locked_operation.error_code = code
        locked_operation.hostaway_reservation_id = hostaway_reservation_id
        locked_operation.completed_at = now
        locked_operation.save()
    reservation.refresh_from_db()
    operation.refresh_from_db()
    return ReservationCreationOutcome(code, reservation, operation)


def _reservation_fingerprint(reservation: Reservation) -> str:
    values: tuple[Any, ...] = (
        reservation.pk,
        reservation.property_id,
        reservation.hostaway_listing_map_id,
        reservation.check_in,
        reservation.check_out,
        reservation.guests,
        reservation.currency,
        reservation.total_price,
    )
    return salted_hmac("hostaway-reservation-request.v1", repr(values)).hexdigest()


def _request_fingerprint(
    reservation: Reservation,
    availability: AvailabilityResult,
) -> str:
    quote = availability.quote
    assert quote is not None
    component_values = tuple(
        (item.type, item.name, item.quantity, item.value, item.total) for item in quote.components
    )
    values = (
        _reservation_fingerprint(reservation),
        quote.total_price,
        quote.currency,
        component_values,
    )
    return salted_hmac("hostaway-reservation-request-price.v1", repr(values)).hexdigest()
