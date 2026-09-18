"""Safe, local-only preparation of owner-created booking drafts."""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from datetime import datetime, time
from decimal import Decimal
from typing import Any

from django.db import transaction
from django.utils import timezone
from django.utils.crypto import salted_hmac

from apps.core.phone_numbers import normalize_phone_number
from apps.payments.currency import CurrencyError, CurrencyService

from .models import BookingIntent, BookingQuote, ManualBookingDraft, Reservation
from .services.availability import AvailabilityRequest, AvailabilityResult, AvailabilityService
from .services.booking import create_quote_for_property
from .services.hostaway_booking import HostawayBookingService, prepare_local_reservation
from .signing import verify_quote_fingerprint


@dataclass(frozen=True, slots=True)
class ManualBookingDraftCreation:
    code: str
    availability: AvailabilityResult
    draft: ManualBookingDraft | None = None


@dataclass(frozen=True, slots=True)
class ManualBookingDraftFinalization:
    code: str
    draft: ManualBookingDraft | None = None


@dataclass(frozen=True, slots=True)
class ManualBookingDraftRecheck:
    code: str
    availability: AvailabilityResult
    draft: ManualBookingDraft | None = None


@dataclass(frozen=True, slots=True)
class ManualBookingHostawayCreation:
    code: str
    draft: ManualBookingDraft | None = None
    reservation: Reservation | None = None


def _owner_quote_hash(actor: object) -> str:
    material = f"{getattr(actor, 'pk', 'owner')}:{secrets.token_urlsafe(24)}"
    return salted_hmac("manual-booking-draft.v1", material).hexdigest()


def create_manual_booking_draft(
    *,
    property_obj: object,
    check_in: object,
    check_out: object,
    guests: object,
    actor: object,
    availability_service: AvailabilityService | None = None,
) -> ManualBookingDraftCreation:
    """Check Hostaway live, then persist a local draft without provider writes."""

    request = AvailabilityRequest(
        property=property_obj,
        check_in=check_in,
        check_out=check_out,
        guests=guests,
    )
    if availability_service is None:
        with AvailabilityService() as service:
            availability = service.check(request, bypass_cache=True)
    else:
        availability = availability_service.check(request, bypass_cache=True)

    if not availability.is_available or availability.quote is None:
        return ManualBookingDraftCreation("unavailable", availability)

    try:
        with transaction.atomic():
            quote = create_quote_for_property(
                availability,
                property_obj=property_obj,
                session_hash=_owner_quote_hash(actor),
                selected_display_currency=availability.quote.currency,
            )
            draft = ManualBookingDraft(
                quote=quote,
                property=property_obj,
                check_in=quote.check_in,
                check_out=quote.check_out,
                nights=quote.nights,
                guests=quote.guests,
                currency=quote.currency,
                system_total_price=quote.total_price,
                final_total_price=quote.total_price,
                payment_amount_sar=quote.payment_amount_sar,
                selected_display_currency=quote.selected_display_currency,
                exchange_rate_snapshot=dict(quote.exchange_rate_snapshot),
                price_source=ManualBookingDraft.PriceSource.SYSTEM,
                availability_checked_at=timezone.now(),
                expires_at=quote.expires_at,
                created_by=actor,
            )
            draft.full_clean()
            draft.save()
    except (CurrencyError, ValueError):
        return ManualBookingDraftCreation("currency_unavailable", availability)
    return ManualBookingDraftCreation("created", availability, draft)


def recheck_manual_booking_draft(
    *,
    draft_id: object,
    actor: object,
    availability_service: AvailabilityService | None = None,
) -> ManualBookingDraftRecheck:
    """Revalidate one saved draft without treating it as a held reservation.

    A manual draft never reserves inventory.  Rechecking therefore asks
    Hostaway again for the draft's saved property, dates and guest count, then
    replaces its expired quote only when the stay is still genuinely available.
    """

    draft = ManualBookingDraft.objects.select_related("property").filter(pk=draft_id).first()
    if draft is None:
        unavailable = AvailabilityResult(False, "unavailable_dates", "", "", 0)
        return ManualBookingDraftRecheck("not_found", unavailable)
    if draft.status in {
        ManualBookingDraft.Status.CANCELLED,
        ManualBookingDraft.Status.BOOKED_AWAITING_PAYMENT,
    }:
        unavailable = AvailabilityResult(False, "unavailable_dates", "", "", draft.nights)
        return ManualBookingDraftRecheck("not_recheckable", unavailable, draft)

    request = AvailabilityRequest(
        property=draft.property,
        check_in=draft.check_in,
        check_out=draft.check_out,
        guests=draft.guests,
    )
    if availability_service is None:
        with AvailabilityService() as service:
            availability = service.check(request, bypass_cache=True)
    else:
        availability = availability_service.check(request, bypass_cache=True)
    if not availability.is_available or availability.quote is None:
        return ManualBookingDraftRecheck("unavailable", availability, draft)

    try:
        with transaction.atomic():
            locked = (
                ManualBookingDraft.objects.select_for_update()
                .select_related("quote", "property")
                .filter(pk=draft_id)
                .first()
            )
            if locked is None:
                return ManualBookingDraftRecheck("not_found", availability)
            if locked.status in {
                ManualBookingDraft.Status.CANCELLED,
                ManualBookingDraft.Status.BOOKED_AWAITING_PAYMENT,
            }:
                return ManualBookingDraftRecheck("not_recheckable", availability, locked)
            previous_quote = locked.quote
            quote = create_quote_for_property(
                availability,
                property_obj=locked.property,
                session_hash=_owner_quote_hash(actor),
                selected_display_currency=availability.quote.currency,
            )
            locked.quote = quote
            locked.currency = quote.currency
            locked.system_total_price = quote.total_price
            locked.final_total_price = quote.total_price
            locked.payment_amount_sar = quote.payment_amount_sar
            locked.selected_display_currency = quote.selected_display_currency
            locked.exchange_rate_snapshot = dict(quote.exchange_rate_snapshot)
            locked.price_source = ManualBookingDraft.PriceSource.SYSTEM
            locked.price_override_reason = ""
            locked.status = ManualBookingDraft.Status.QUOTED
            locked.availability_checked_at = timezone.now()
            locked.expires_at = quote.expires_at
            locked.full_clean()
            locked.save()
            previous_quote.status = BookingQuote.Status.INVALIDATED
            previous_quote.invalidated_at = timezone.now()
            previous_quote.save(update_fields=["status", "invalidated_at", "updated_at"])
    except (CurrencyError, ValueError):
        return ManualBookingDraftRecheck("currency_unavailable", availability, draft)
    return ManualBookingDraftRecheck("rechecked", availability, locked)


def finalize_manual_booking_draft(
    *,
    draft_id: object,
    guest_data: dict[str, Any],
    final_total_price: Decimal,
) -> ManualBookingDraftFinalization:
    """Save guest data and a traceable final price; never charge or book externally."""

    with transaction.atomic():
        draft = (
            ManualBookingDraft.objects.select_for_update()
            .select_related("quote", "property")
            .filter(pk=draft_id)
            .first()
        )
        if draft is None:
            return ManualBookingDraftFinalization("not_found")
        if draft.status != ManualBookingDraft.Status.QUOTED:
            return ManualBookingDraftFinalization("not_editable", draft)
        if (
            draft.quote.status != BookingQuote.Status.ACTIVE
            or draft.quote.is_expired
            or not verify_quote_fingerprint(draft.quote)
        ):
            draft.status = ManualBookingDraft.Status.EXPIRED
            draft.save(update_fields=["status", "updated_at"])
            return ManualBookingDraftFinalization("quote_expired", draft)

        # Owner-entered totals use two currency decimals. The original Hostaway
        # quote remains untouched for audit, even if that provider uses 4 places.
        final_total = Decimal(final_total_price).quantize(Decimal("0.01"))
        is_manual_price = final_total != draft.system_total_price

        if is_manual_price:
            try:
                with CurrencyService() as currency_service:
                    final_price = currency_service.create_quote(
                        source_amount=final_total,
                        source_currency=draft.currency,
                        display_currency=draft.currency,
                        quote_created_at=timezone.now(),
                        quote_expires_at=draft.expires_at,
                    )
            except CurrencyError:
                return ManualBookingDraftFinalization("currency_unavailable", draft)
            payment_amount_sar = final_price.payment_amount_sar
            exchange_rate_snapshot = dict(final_price.snapshot)
            selected_display_currency = final_price.display_currency
            price_source = ManualBookingDraft.PriceSource.MANUAL_OVERRIDE
        else:
            payment_amount_sar = draft.quote.payment_amount_sar
            exchange_rate_snapshot = dict(draft.quote.exchange_rate_snapshot)
            selected_display_currency = draft.quote.selected_display_currency
            price_source = ManualBookingDraft.PriceSource.SYSTEM

        draft.guest_first_name = str(guest_data["guest_first_name"])
        draft.guest_last_name = str(guest_data["guest_last_name"])
        draft.guest_email = str(guest_data["guest_email"])
        draft.guest_phone = str(guest_data["guest_phone"])
        draft.special_requests = ""
        draft.final_total_price = final_total
        draft.payment_amount_sar = payment_amount_sar
        draft.selected_display_currency = selected_display_currency
        draft.exchange_rate_snapshot = exchange_rate_snapshot
        draft.price_source = price_source
        draft.price_override_reason = ""
        draft.status = ManualBookingDraft.Status.READY_FOR_PAYMENT
        draft.full_clean()
        draft.save()
    return ManualBookingDraftFinalization("ready_for_payment", draft)


def create_manual_booking_in_hostaway(
    *,
    draft_id: object,
    booking_service: HostawayBookingService | None = None,
) -> ManualBookingHostawayCreation:
    """Create the owner-approved stay before a manual payment link is issued.

    The real Hostaway reservation blocks the dates.  The booking remains unpaid
    locally and in Hostaway; this function never creates a HyperPay charge or
    marks the guest as paid.
    """

    with transaction.atomic():
        draft = (
            ManualBookingDraft.objects.select_for_update()
            .select_related("quote", "property")
            .filter(pk=draft_id)
            .first()
        )
        if draft is None:
            return ManualBookingHostawayCreation("not_found")
        if draft.status == ManualBookingDraft.Status.BOOKED_AWAITING_PAYMENT:
            reservation = Reservation.objects.filter(booking_intent__quote=draft.quote).first()
            return ManualBookingHostawayCreation("already_created", draft, reservation)
        if draft.status != ManualBookingDraft.Status.READY_FOR_PAYMENT:
            return ManualBookingHostawayCreation("not_ready", draft)
        if (
            draft.quote.status != BookingQuote.Status.ACTIVE
            or draft.quote.is_expired
            or not verify_quote_fingerprint(draft.quote)
        ):
            draft.status = ManualBookingDraft.Status.EXPIRED
            draft.save(update_fields=["status", "updated_at"])
            return ManualBookingHostawayCreation("quote_expired", draft)

        intent = BookingIntent.objects.filter(quote=draft.quote).first()
        if intent is None:
            now = timezone.now()
            intent = BookingIntent(
                quote=draft.quote,
                property=draft.property,
                check_in=draft.check_in,
                check_out=draft.check_out,
                nights=draft.nights,
                guests=draft.guests,
                currency=draft.currency,
                total_price=draft.final_total_price,
                payment_amount_sar=draft.payment_amount_sar,
                selected_display_currency=draft.selected_display_currency,
                exchange_rate_snapshot=dict(draft.exchange_rate_snapshot),
                guest_first_name=draft.guest_first_name,
                guest_last_name=draft.guest_last_name,
                guest_email=draft.guest_email,
                guest_phone=draft.guest_phone,
                guest_country_code=_guest_country_code(draft.guest_phone),
                billing_street1="Not provided",
                billing_city="Not provided",
                billing_state="Not provided",
                billing_country=_guest_country_code(draft.guest_phone),
                billing_postcode="Not provided",
                language="ar",
                special_requests="",
                status=BookingIntent.Status.AWAITING_PAYMENT,
                idempotency_key=_manual_intent_idempotency_key(draft),
                session_key_hash=draft.quote.session_key_hash,
                terms_accepted_at=now,
                privacy_accepted_at=now,
                marketing_consent=False,
                # A confirmed manual booking stays payable until check-in; it
                # must not inherit the public checkout's 30-minute expiry.
                expires_at=timezone.make_aware(datetime.combine(draft.check_in, time.min)),
            )
            intent.full_clean(validate_unique=False, validate_constraints=False)
            intent.save(force_insert=True)
            draft.quote.status = BookingQuote.Status.CONSUMED
            draft.quote.consumed_at = now
            draft.quote.save(update_fields=["status", "consumed_at", "updated_at"])
        reservation = prepare_local_reservation(intent)

    if booking_service is None:
        with HostawayBookingService() as service:
            outcome = service.create_manual_reservation_before_payment(reservation)
    else:
        outcome = booking_service.create_manual_reservation_before_payment(reservation)

    if outcome.code not in {"confirmed", "awaiting_payment", "already_confirmed"}:
        return ManualBookingHostawayCreation(outcome.code, draft, outcome.reservation)

    with transaction.atomic():
        locked = ManualBookingDraft.objects.select_for_update().get(pk=draft.pk)
        locked.status = ManualBookingDraft.Status.BOOKED_AWAITING_PAYMENT
        locked.save(update_fields=["status", "updated_at"])
    outcome.reservation.refresh_from_db()
    return ManualBookingHostawayCreation("created", locked, outcome.reservation)


def _manual_intent_idempotency_key(draft: ManualBookingDraft) -> str:
    return salted_hmac("manual-booking-intent.v1", str(draft.pk)).hexdigest()


def _guest_country_code(phone: str) -> str:
    """Derive the ISO country from the required E.164 phone, defaulting to SA."""

    try:
        import phonenumbers

        parsed = phonenumbers.parse(normalize_phone_number(phone), None)
        return phonenumbers.region_code_for_number(parsed) or "SA"
    except (ImportError, ValueError):
        return "SA"
