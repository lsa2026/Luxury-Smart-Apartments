"""Local-only reservation modification requests with live revalidation."""

import re
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal

from django.conf import settings
from django.db import transaction
from django.utils import timezone
from django.utils.crypto import constant_time_compare, salted_hmac
from django.utils.html import strip_tags

from apps.integrations.hostaway.exceptions import HostawayError

from ..models import BookingModificationRequest, Reservation
from .availability import (
    AVAILABLE,
    AvailabilityRequest,
    AvailabilityService,
    evaluate_calendar,
)
from .booking import sanitized_components


@dataclass(frozen=True, slots=True)
class ModificationCreation:
    code: str
    request: BookingModificationRequest | None = None


class ModificationService:
    """Creates local requests only; it has no Hostaway write methods."""

    def __init__(self, *, availability_service: AvailabilityService | None = None) -> None:
        self.availability_service = availability_service or AvailabilityService()
        self._owns_service = availability_service is None

    def close(self) -> None:
        if self._owns_service:
            self.availability_service.close()

    def __enter__(self) -> "ModificationService":
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    def create_extension_quote(
        self,
        reservation: Reservation,
        *,
        new_check_out: date,
        session_hash: str,
        reason: str = "",
    ) -> ModificationCreation:
        blocker = _base_blocker(reservation, session_hash)
        if blocker:
            return ModificationCreation(blocker)
        if new_check_out <= reservation.check_out:
            return ModificationCreation("extension_must_add_nights")
        added_nights = (new_check_out - reservation.check_out).days
        if added_nights > settings.BOOKING_EXTENSION_MAX_NIGHTS:
            return ModificationCreation("extension_limit_exceeded")
        assert reservation.property is not None
        try:
            calendar = self.availability_service.fetch_calendar(
                property_obj=reservation.property,
                start_date=reservation.check_out,
                end_date=new_check_out,
                bypass_cache=True,
            )
            reason_code = evaluate_calendar(
                calendar.document.days,
                check_in=reservation.check_out,
                check_out=new_check_out,
            )
            if reason_code != AVAILABLE:
                return ModificationCreation(reason_code)
            quote = self.availability_service.client.calculate_price(
                reservation.property.hostaway_listing_id,
                check_in=reservation.check_in,
                check_out=new_check_out,
                guests=reservation.guests,
                fallback_currency=reservation.currency,
            )
        except (HostawayError, ValueError):
            return ModificationCreation("hostaway_temporarily_unavailable")
        return self._persist_priced_request(
            reservation,
            request_type=BookingModificationRequest.RequestType.EXTEND_STAY,
            new_check_in=reservation.check_in,
            new_check_out=new_check_out,
            new_guests=reservation.guests,
            session_hash=session_hash,
            reason=reason,
            quote=quote,
        )

    def create_change_quote(
        self,
        reservation: Reservation,
        *,
        new_check_in: date,
        new_check_out: date,
        new_guests: int,
        session_hash: str,
        reason: str = "",
    ) -> ModificationCreation:
        blocker = _base_blocker(reservation, session_hash)
        if blocker:
            return ModificationCreation(blocker)
        if new_check_out <= new_check_in:
            return ModificationCreation("invalid_dates")
        if new_guests <= 0:
            return ModificationCreation("invalid_guests")
        assert reservation.property is not None
        capacity = reservation.property.person_capacity
        if capacity and new_guests > capacity:
            return ModificationCreation("capacity_exceeded")
        if new_check_in == reservation.check_in and new_check_out == reservation.check_out:
            request_type = BookingModificationRequest.RequestType.CHANGE_GUESTS
        else:
            request_type = BookingModificationRequest.RequestType.CHANGE_DATES
        result = self.availability_service.check(
            AvailabilityRequest(
                property=reservation.property,
                check_in=new_check_in,
                check_out=new_check_out,
                guests=new_guests,
            ),
            bypass_cache=True,
        )
        if not result.is_available or result.quote is None:
            return ModificationCreation(result.reason_code)
        return self._persist_priced_request(
            reservation,
            request_type=request_type,
            new_check_in=new_check_in,
            new_check_out=new_check_out,
            new_guests=new_guests,
            session_hash=session_hash,
            reason=reason,
            quote=result.quote,
        )

    def create_cancellation_request(
        self,
        reservation: Reservation,
        *,
        session_hash: str,
        reason: str = "",
    ) -> ModificationCreation:
        if not settings.BOOKING_CANCELLATION_REQUEST_ENABLED:
            return ModificationCreation("cancellation_requests_disabled")
        blocker = _base_blocker(reservation, session_hash)
        if blocker:
            return ModificationCreation(blocker)
        key = _idempotency_key(
            reservation,
            BookingModificationRequest.RequestType.CANCEL_RESERVATION,
            None,
            None,
            None,
            session_hash,
        )
        existing = BookingModificationRequest.objects.filter(idempotency_key=key).first()
        if existing:
            return ModificationCreation("idempotent", existing)
        request = BookingModificationRequest(
            reservation=reservation,
            request_type=BookingModificationRequest.RequestType.CANCEL_RESERVATION,
            status=BookingModificationRequest.Status.PENDING_ADMIN_APPROVAL,
            old_check_in=reservation.check_in,
            old_check_out=reservation.check_out,
            old_guests=reservation.guests,
            old_total=reservation.total_price,
            price_difference=Decimal("0"),
            currency=reservation.currency,
            reason=_clean_reason(reason),
            quote_snapshot={},
            idempotency_key=key,
            session_key_hash=session_hash,
            expires_at=BookingModificationRequest.default_expiry(),
        )
        request.full_clean()
        with transaction.atomic():
            request.save(force_insert=True)
            from apps.notifications.services.events import handle_modification_created

            transaction.on_commit(lambda: handle_modification_created(request.pk))
        return ModificationCreation("created", request)

    @staticmethod
    def _persist_priced_request(
        reservation: Reservation,
        *,
        request_type: str,
        new_check_in: date,
        new_check_out: date,
        new_guests: int,
        session_hash: str,
        reason: str,
        quote: object,
    ) -> ModificationCreation:
        if quote.currency != reservation.currency:
            return ModificationCreation("currency_changed")
        difference = quote.total_price - reservation.total_price
        key = _idempotency_key(
            reservation,
            request_type,
            new_check_in,
            new_check_out,
            new_guests,
            session_hash,
        )
        existing = BookingModificationRequest.objects.filter(idempotency_key=key).first()
        if existing:
            return ModificationCreation("idempotent", existing)
        if difference > 0:
            status = BookingModificationRequest.Status.AWAITING_PAYMENT
        else:
            status = BookingModificationRequest.Status.PENDING_ADMIN_APPROVAL
        cutoff = timezone.now() + timedelta(hours=settings.BOOKING_MODIFICATION_CUTOFF_HOURS)
        if reservation.check_in <= timezone.localtime(cutoff).date():
            status = BookingModificationRequest.Status.PENDING_ADMIN_APPROVAL
        snapshot = {
            "price_version": 2,
            "components": sanitized_components(quote),
            "operational_components": [
                {
                    "listing_fee_setting_id": item.listing_fee_setting_id,
                    "type": item.type[:50],
                    "name": item.name[:100],
                    "title": item.title[:200],
                    "alias": item.alias[:100],
                    "quantity": item.quantity,
                    "value": format(item.value, "f"),
                    "total": format(item.total, "f") if item.total is not None else None,
                    "is_included_in_total": item.is_included_in_total,
                    "is_mandatory": item.is_mandatory,
                    "is_deleted": item.is_deleted,
                }
                for item in quote.components
            ],
            "calculated_at": quote.calculated_at.isoformat(),
        }
        request = BookingModificationRequest(
            reservation=reservation,
            request_type=request_type,
            status=status,
            old_check_in=reservation.check_in,
            old_check_out=reservation.check_out,
            new_check_in=new_check_in,
            new_check_out=new_check_out,
            old_guests=reservation.guests,
            new_guests=new_guests,
            old_total=reservation.total_price,
            new_total=quote.total_price,
            price_difference=difference,
            currency=quote.currency,
            reason=_clean_reason(reason),
            quote_snapshot=snapshot,
            idempotency_key=key,
            session_key_hash=session_hash,
            expires_at=BookingModificationRequest.default_expiry(),
        )
        request.full_clean()
        with transaction.atomic():
            request.save(force_insert=True)
            from apps.notifications.services.events import handle_modification_created

            transaction.on_commit(lambda: handle_modification_created(request.pk))
        return ModificationCreation("created", request)


def _base_blocker(reservation: Reservation, session_hash: str) -> str:
    if reservation.normalized_status != Reservation.Status.CONFIRMED:
        return "reservation_not_confirmed"
    if reservation.source_type != Reservation.SourceType.DIRECT_WEBSITE:
        return "external_channel_requires_admin"
    if reservation.booking_intent_id is None or reservation.property_id is None:
        return "direct_reservation_context_missing"
    if not constant_time_compare(reservation.booking_intent.session_key_hash, session_hash):
        return "not_found"
    return ""


def _idempotency_key(
    reservation: Reservation,
    request_type: str,
    check_in: date | None,
    check_out: date | None,
    guests: int | None,
    session_hash: str,
) -> str:
    values = (
        reservation.pk,
        request_type,
        check_in,
        check_out,
        guests,
        session_hash,
    )
    return salted_hmac("booking-modification.v1", repr(values)).hexdigest()


def _clean_reason(value: str) -> str:
    without_markup = strip_tags(value)
    return re.sub(r"\s+", " ", without_markup).strip()[:1000]
