"""Future Hostaway modification writes, fully feature-gated and idempotent."""

import secrets
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

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
from apps.integrations.hostaway.modification_validators import (
    HostawayReservationCancellationRequest,
    HostawayReservationUpdateRequest,
)
from apps.integrations.hostaway.reservation_validators import (
    ReservationFinanceField,
    merge_hostaway_payment_status,
)

from ..models import (
    BookingModificationRequest,
    HostawayModificationOperation,
    Reservation,
)


@dataclass(frozen=True, slots=True)
class ModificationExecution:
    code: str
    request: BookingModificationRequest
    operation: HostawayModificationOperation | None = None


class HostawayModificationService:
    """Executes at most one future PUT, then reconciles through GET in the client."""

    def __init__(self, *, client: HostawayClient | None = None) -> None:
        self.client = client or HostawayClient(
            timeout=settings.HOSTAWAY_RESERVATION_REQUEST_TIMEOUT_SECONDS
        )
        self._owns_client = client is None

    def close(self) -> None:
        if self._owns_client:
            self.client.close()

    def __enter__(self) -> "HostawayModificationService":
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    def execute(self, modification: BookingModificationRequest) -> ModificationExecution:
        reservation = modification.reservation
        if modification.status == BookingModificationRequest.Status.UNKNOWN:
            operation = modification.hostaway_operations.filter(
                status=HostawayModificationOperation.Status.UNKNOWN
            ).first()
            return ModificationExecution(
                HostawayModificationOperation.Status.UNKNOWN,
                modification,
                operation,
            )
        blocker = _execution_blocker(modification, reservation)
        if blocker:
            return ModificationExecution(blocker, modification)
        operation, can_send = _prepare_operation(modification)
        if not can_send:
            return ModificationExecution(operation.status, modification, operation)
        try:
            if (
                modification.request_type
                == BookingModificationRequest.RequestType.CANCEL_RESERVATION
            ):
                snapshot = self.client.cancel_reservation(
                    reservation.hostaway_reservation_id,
                    HostawayReservationCancellationRequest(cancelled_by="guest"),
                )
            else:
                update_request = _build_update_request(modification)
                snapshot = self.client.update_reservation(
                    reservation.hostaway_reservation_id,
                    update_request,
                    extension=(
                        modification.request_type
                        == BookingModificationRequest.RequestType.EXTEND_STAY
                    ),
                )
        except (HostawayNetworkError, HostawayServerError):
            return _finish(
                modification,
                operation,
                request_status=BookingModificationRequest.Status.UNKNOWN,
                operation_status=HostawayModificationOperation.Status.UNKNOWN,
                code="hostaway_modification_uncertain",
            )
        except (
            HostawayAuthenticationError,
            HostawayAvailabilityError,
            HostawayConfigurationError,
            HostawayRateLimitError,
            HostawayResponseError,
            ValueError,
        ):
            return _finish(
                modification,
                operation,
                request_status=BookingModificationRequest.Status.FAILED,
                operation_status=HostawayModificationOperation.Status.FAILED,
                code="hostaway_modification_rejected",
            )
        if not _snapshot_matches(modification, snapshot):
            return _finish(
                modification,
                operation,
                request_status=BookingModificationRequest.Status.UNKNOWN,
                operation_status=HostawayModificationOperation.Status.UNKNOWN,
                code="hostaway_reconciliation_mismatch",
            )
        with transaction.atomic():
            locked_request = BookingModificationRequest.objects.select_for_update().get(
                pk=modification.pk
            )
            locked_reservation = Reservation.objects.select_for_update().get(pk=reservation.pk)
            locked_operation = HostawayModificationOperation.objects.select_for_update().get(
                pk=operation.pk
            )
            now = timezone.now()
            locked_reservation.check_in = snapshot.check_in
            locked_reservation.check_out = snapshot.check_out
            locked_reservation.nights = (snapshot.check_out - snapshot.check_in).days
            locked_reservation.guests = snapshot.guests
            locked_reservation.total_price = snapshot.total_price
            locked_reservation.currency = snapshot.currency
            locked_reservation.hostaway_status = snapshot.status
            locked_reservation.payment_status = merge_hostaway_payment_status(
                locked_reservation.payment_status,
                snapshot.payment_status,
            )
            locked_reservation.source_updated_at = snapshot.updated_at
            locked_reservation.last_synced_at = now
            if snapshot.status.casefold() in {"cancelled", "canceled"}:
                locked_reservation.normalized_status = Reservation.Status.CANCELLED
                locked_reservation.cancelled_at = now
                locked_reservation.confirmed_at = None
            else:
                locked_reservation.normalized_status = Reservation.Status.MODIFIED
            locked_reservation.full_clean()
            locked_reservation.save()
            locked_request.status = BookingModificationRequest.Status.COMPLETED
            locked_request.completed_at = now
            locked_request.save()
            locked_operation.status = HostawayModificationOperation.Status.SUCCEEDED
            locked_operation.completed_at = now
            locked_operation.error_code = ""
            locked_operation.save()
            from apps.notifications.services.events import handle_modification_completed

            transaction.on_commit(
                lambda: handle_modification_completed(locked_request.pk)
            )
        modification.refresh_from_db()
        operation.refresh_from_db()
        return ModificationExecution("completed", modification, operation)


def _execution_blocker(
    modification: BookingModificationRequest,
    reservation: Reservation,
) -> str:
    if modification.status != BookingModificationRequest.Status.READY_FOR_HOSTAWAY:
        return "modification_not_ready"
    if reservation.hostaway_reservation_id is None:
        return "hostaway_reservation_id_missing"
    if reservation.source_type != Reservation.SourceType.DIRECT_WEBSITE:
        return "external_channel_modification_blocked"
    if modification.request_type == BookingModificationRequest.RequestType.CANCEL_RESERVATION:
        if not settings.HOSTAWAY_LIVE_CANCELLATION_ENABLED:
            return "hostaway_live_cancellation_disabled"
        if not settings.BOOKING_AUTOMATIC_CANCELLATION_ENABLED:
            return "automatic_cancellation_disabled"
    else:
        if not settings.HOSTAWAY_LIVE_MODIFICATION_ENABLED:
            return "hostaway_live_modification_disabled"
        if (
            modification.request_type == BookingModificationRequest.RequestType.EXTEND_STAY
            and not settings.HOSTAWAY_LIVE_EXTENSION_ENABLED
        ):
            return "hostaway_live_extension_disabled"
    return ""


def _prepare_operation(
    modification: BookingModificationRequest,
) -> tuple[HostawayModificationOperation, bool]:
    operation_type = {
        BookingModificationRequest.RequestType.EXTEND_STAY: (
            HostawayModificationOperation.OperationType.EXTEND_STAY
        ),
        BookingModificationRequest.RequestType.CHANGE_DATES: (
            HostawayModificationOperation.OperationType.UPDATE_DATES
        ),
        BookingModificationRequest.RequestType.CHANGE_GUESTS: (
            HostawayModificationOperation.OperationType.UPDATE_GUESTS
        ),
        BookingModificationRequest.RequestType.CANCEL_RESERVATION: (
            HostawayModificationOperation.OperationType.CANCEL_RESERVATION
        ),
    }[modification.request_type]
    fingerprint = salted_hmac(
        "hostaway-modification-request.v1",
        repr(
            (
                modification.pk,
                modification.new_check_in,
                modification.new_check_out,
                modification.new_guests,
                modification.new_total,
            )
        ),
    ).hexdigest()
    with transaction.atomic():
        locked = BookingModificationRequest.objects.select_for_update().get(pk=modification.pk)
        operation, created = (
            HostawayModificationOperation.objects.select_for_update().get_or_create(
                modification_request=locked,
                operation_type=operation_type,
                defaults={
                    "idempotency_key": secrets.token_urlsafe(32),
                    "request_fingerprint": fingerprint,
                    "status": HostawayModificationOperation.Status.PREPARED,
                    "hostaway_reservation_id": locked.reservation.hostaway_reservation_id,
                },
            )
        )
        if not created and (
            operation.status
            in {
                HostawayModificationOperation.Status.SUCCEEDED,
                HostawayModificationOperation.Status.IN_PROGRESS,
                HostawayModificationOperation.Status.UNKNOWN,
            }
            or operation.attempt_count > 0
        ):
            return operation, False
        operation.status = HostawayModificationOperation.Status.IN_PROGRESS
        operation.attempt_count += 1
        operation.started_at = timezone.now()
        operation.request_fingerprint = fingerprint
        operation.error_code = ""
        operation.save()
        locked.status = BookingModificationRequest.Status.PROCESSING
        locked.save(update_fields=["status", "updated_at"])
        return operation, True


def _build_update_request(
    modification: BookingModificationRequest,
) -> HostawayReservationUpdateRequest:
    reservation = modification.reservation
    if reservation.hostaway_listing_map_id is None:
        raise ValueError("listing_map_id_not_verified")
    if (
        modification.new_check_in is None
        or modification.new_check_out is None
        or modification.new_guests is None
        or modification.new_total is None
    ):
        raise ValueError("modification_quote_incomplete")
    components = modification.quote_snapshot.get("operational_components")
    if not isinstance(components, list) or not components:
        raise ValueError("price_components_missing")
    finance_fields = tuple(_finance_field(item) for item in components)
    return HostawayReservationUpdateRequest(
        listing_map_id=reservation.hostaway_listing_map_id,
        check_in=modification.new_check_in,
        check_out=modification.new_check_out,
        guests=modification.new_guests,
        currency=modification.currency,
        total_price=modification.new_total,
        finance_fields=finance_fields,
    )


def _finance_field(item: object) -> ReservationFinanceField:
    if not isinstance(item, dict):
        raise ValueError("price_component_invalid")
    try:
        value = Decimal(str(item["value"]))
        total = Decimal(str(item["total"]))
    except (KeyError, InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError("price_component_invalid") from exc
    required = (
        item.get("is_included_in_total"),
        item.get("is_mandatory"),
        item.get("is_deleted"),
    )
    if any(value is None for value in required):
        raise ValueError("price_component_flags_missing")
    return ReservationFinanceField(
        listing_fee_setting_id=item.get("listing_fee_setting_id"),
        type=str(item.get("type", ""))[:50],
        name=str(item.get("name", ""))[:100],
        title=str(item.get("title", ""))[:200],
        alias=str(item.get("alias", ""))[:100],
        quantity=item.get("quantity"),
        value=value,
        total=total,
        is_included_in_total_price=bool(item["is_included_in_total"]),
        is_overridden_by_user=False,
        is_mandatory=bool(item["is_mandatory"]),
        is_deleted=bool(item["is_deleted"]),
    )


def _snapshot_matches(modification: BookingModificationRequest, snapshot: object) -> bool:
    if modification.request_type == BookingModificationRequest.RequestType.CANCEL_RESERVATION:
        return snapshot.status.casefold() in {"cancelled", "canceled"}
    return (
        snapshot.check_in == modification.new_check_in
        and snapshot.check_out == modification.new_check_out
        and snapshot.guests == modification.new_guests
        and snapshot.currency == modification.currency
        and snapshot.total_price == modification.new_total
    )


def _finish(
    modification: BookingModificationRequest,
    operation: HostawayModificationOperation,
    *,
    request_status: str,
    operation_status: str,
    code: str,
) -> ModificationExecution:
    with transaction.atomic():
        locked_request = BookingModificationRequest.objects.select_for_update().get(
            pk=modification.pk
        )
        locked_operation = HostawayModificationOperation.objects.select_for_update().get(
            pk=operation.pk
        )
        locked_request.status = request_status
        locked_request.save(update_fields=["status", "updated_at"])
        locked_operation.status = operation_status
        locked_operation.error_code = code
        locked_operation.completed_at = timezone.now()
        locked_operation.save()
    modification.refresh_from_db()
    operation.refresh_from_db()
    return ModificationExecution(code, modification, operation)
