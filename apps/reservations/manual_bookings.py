"""Safe, local-only preparation of owner-created booking drafts."""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from django.db import transaction
from django.utils import timezone
from django.utils.crypto import salted_hmac

from apps.payments.currency import CurrencyError, CurrencyService

from .models import BookingQuote, ManualBookingDraft
from .services.availability import AvailabilityRequest, AvailabilityResult, AvailabilityService
from .services.booking import create_quote_for_property
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

        final_total = Decimal(final_total_price)
        is_manual_price = final_total != draft.system_total_price
        reason = str(guest_data.get("price_override_reason") or "").strip()
        if is_manual_price and len(reason) < 10:
            return ManualBookingDraftFinalization("override_reason_required", draft)

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
            reason = ""

        draft.guest_first_name = str(guest_data["guest_first_name"])
        draft.guest_last_name = str(guest_data["guest_last_name"])
        draft.guest_email = str(guest_data["guest_email"])
        draft.guest_phone = str(guest_data["guest_phone"])
        draft.special_requests = str(guest_data.get("special_requests") or "")
        draft.final_total_price = final_total
        draft.payment_amount_sar = payment_amount_sar
        draft.selected_display_currency = selected_display_currency
        draft.exchange_rate_snapshot = exchange_rate_snapshot
        draft.price_source = price_source
        draft.price_override_reason = reason
        draft.status = ManualBookingDraft.Status.READY_FOR_PAYMENT
        draft.full_clean()
        draft.save()
    return ManualBookingDraftFinalization("ready_for_payment", draft)
