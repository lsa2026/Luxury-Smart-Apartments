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

from apps.integrations.hostaway.availability_validators import PriceQuote
from apps.integrations.hostaway.exceptions import HostawayError
from apps.payments.currency import CurrencyError, CurrencyService

from ..models import BookingModificationRequest, Reservation
from .availability import (
    AVAILABLE,
    AvailabilityRequest,
    AvailabilityService,
    evaluate_calendar,
)
from .booking import sanitized_components
from .host_policy import check_in_datetime, policy_blocker


@dataclass(frozen=True, slots=True)
class ModificationCreation:
    code: str
    request: BookingModificationRequest | None = None


@dataclass(frozen=True, slots=True)
class ModificationRevalidation:
    code: str
    quote: PriceQuote | None = None


class ModificationService:
    """Creates local requests only; it has no Hostaway write methods."""

    def __init__(
        self,
        *,
        availability_service: AvailabilityService | None = None,
        currency_service: CurrencyService | None = None,
    ) -> None:
        self.availability_service = availability_service or AvailabilityService()
        self._owns_service = availability_service is None
        self.currency_service = currency_service or CurrencyService()
        self._owns_currency_service = currency_service is None

    def close(self) -> None:
        if self._owns_service:
            self.availability_service.close()
        if self._owns_currency_service:
            self.currency_service.close()

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
        refusal = policy_blocker(
            reservation.property,
            check_in=reservation.check_in,
            check_out=new_check_out,
        )
        if refusal:
            return ModificationCreation(refusal)
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
                bypass_currency_cache=True,
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
        refusal = policy_blocker(
            reservation.property,
            check_in=new_check_in,
            check_out=new_check_out,
        )
        if refusal:
            return ModificationCreation(refusal)
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
            status=(
                BookingModificationRequest.Status.READY_FOR_HOSTAWAY
                if settings.BOOKING_AUTOMATIC_MODIFICATION_APPROVAL
                and settings.BOOKING_AUTOMATIC_CANCELLATION_ENABLED
                else BookingModificationRequest.Status.PENDING_ADMIN_APPROVAL
            ),
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

    def revalidate_for_payment(
        self,
        modification: BookingModificationRequest,
    ) -> ModificationRevalidation:
        """Recheck inventory and the full trusted price immediately before payment."""

        reservation = modification.reservation
        if (
            modification.status != BookingModificationRequest.Status.AWAITING_PAYMENT
            or modification.is_expired
            or reservation.normalized_status != Reservation.Status.CONFIRMED
            or modification.new_check_in is None
            or modification.new_check_out is None
            or modification.new_guests is None
            or modification.new_total is None
            or modification.old_check_in != reservation.check_in
            or modification.old_check_out != reservation.check_out
            or modification.old_guests != reservation.guests
            or modification.old_total != reservation.total_price
            or modification.currency != reservation.currency
            or reservation.property is None
        ):
            return ModificationRevalidation("modification_not_payable")
        try:
            if (
                modification.request_type
                == BookingModificationRequest.RequestType.EXTEND_STAY
            ):
                calendar = self.availability_service.fetch_calendar(
                    property_obj=reservation.property,
                    start_date=reservation.check_out,
                    end_date=modification.new_check_out,
                    bypass_cache=True,
                )
                reason_code = evaluate_calendar(
                    calendar.document.days,
                    check_in=reservation.check_out,
                    check_out=modification.new_check_out,
                )
                if reason_code != AVAILABLE:
                    return ModificationRevalidation(reason_code)
                quote = self.availability_service.client.calculate_price(
                    reservation.property.hostaway_listing_id,
                    check_in=modification.new_check_in,
                    check_out=modification.new_check_out,
                    guests=modification.new_guests,
                    bypass_currency_cache=True,
                )
            else:
                result = self.availability_service.check(
                    AvailabilityRequest(
                        property=reservation.property,
                        check_in=modification.new_check_in,
                        check_out=modification.new_check_out,
                        guests=modification.new_guests,
                    ),
                    bypass_cache=True,
                )
                if not result.is_available or result.quote is None:
                    return ModificationRevalidation(result.reason_code)
                quote = result.quote
        except (HostawayError, ValueError):
            return ModificationRevalidation("hostaway_temporarily_unavailable")

        if quote.currency != modification.currency:
            return ModificationRevalidation("currency_changed", quote)
        if (
            quote.total_price != modification.new_total
            or quote.total_price - reservation.total_price
            != modification.price_difference
        ):
            return ModificationRevalidation("price_changed", quote)
        return ModificationRevalidation("ready", quote)

    def _persist_priced_request(
        self,
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
        elif settings.BOOKING_AUTOMATIC_MODIFICATION_APPROVAL:
            # A decrease owes the guest money rather than collecting any, so it
            # applies straight away and leaves a refund obligation behind.
            status = BookingModificationRequest.Status.READY_FOR_HOSTAWAY
        else:
            status = BookingModificationRequest.Status.PENDING_ADMIN_APPROVAL
        # Measured to the hour the guest actually arrives, in the listing's own
        # timezone, so the window does not close a day early or late.
        arrival = check_in_datetime(reservation.property, reservation.check_in)
        cutoff = timedelta(hours=settings.BOOKING_MODIFICATION_CUTOFF_HOURS)
        if (
            not settings.BOOKING_AUTOMATIC_MODIFICATION_APPROVAL
            and arrival - timezone.now() <= cutoff
        ):
            status = BookingModificationRequest.Status.PENDING_ADMIN_APPROVAL
        now = timezone.now()
        expires_at = BookingModificationRequest.default_expiry()
        payment_amount_sar = None
        fx_snapshot: dict[str, object] = {}
        if difference > 0:
            display_currency = (
                reservation.booking_intent.selected_display_currency
                if reservation.booking_intent_id
                and reservation.booking_intent.selected_display_currency
                else "SAR"
            )
            try:
                currency_quote = self.currency_service.create_quote(
                    source_amount=difference,
                    source_currency=quote.currency,
                    display_currency=display_currency,
                    quote_created_at=now,
                    quote_expires_at=expires_at,
                )
            except CurrencyError:
                return ModificationCreation("fx_unavailable")
            payment_amount_sar = currency_quote.payment_amount_sar
            fx_snapshot = dict(currency_quote.snapshot)
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
            "fx": fx_snapshot,
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
            payment_amount_sar=payment_amount_sar,
            currency=quote.currency,
            reason=_clean_reason(reason),
            quote_snapshot=snapshot,
            idempotency_key=key,
            session_key_hash=session_hash,
            expires_at=expires_at,
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
