"""Build the minimal documented Hostaway reservation payload from trusted state."""

from dataclasses import replace
from decimal import Decimal

from django.conf import settings

from apps.integrations.hostaway.availability_validators import PriceQuote
from apps.integrations.hostaway.reservation_validators import (
    HostawayReservationCreateRequest,
    ReservationFinanceField,
)
from apps.reservations.models import Reservation


def build_hostaway_reservation_request(
    reservation: Reservation,
    *,
    current_quote: PriceQuote,
    allow_manual_price_override: bool = False,
) -> HostawayReservationCreateRequest:
    """Return an explicit DTO; never accept listing IDs or prices from a browser."""
    intent = reservation.booking_intent
    property_obj = reservation.property
    if intent is None or property_obj is None:
        raise ValueError("direct_reservation_context_missing")
    listing_map_id = property_obj.hostaway_listing_map_id
    if listing_map_id is None:
        raise ValueError("listing_map_id_not_verified")
    channel_id = settings.HOSTAWAY_DIRECT_CHANNEL_ID
    if channel_id is None:
        raise ValueError("direct_channel_id_not_configured")
    if current_quote.listing_id != property_obj.hostaway_listing_id:
        raise ValueError("revalidated_listing_mismatch")
    if (
        current_quote.check_in != reservation.check_in
        or current_quote.check_out != reservation.check_out
        or current_quote.guests != reservation.guests
    ):
        raise ValueError("revalidated_stay_mismatch")
    if current_quote.currency != reservation.currency:
        raise ValueError("revalidated_price_changed")
    finance_fields = tuple(
        ReservationFinanceField.from_price_component(component)
        for component in current_quote.components
    )
    if current_quote.total_price != reservation.total_price:
        if not allow_manual_price_override:
            raise ValueError("revalidated_price_changed")
        finance_fields = _override_price_details(
            finance_fields=finance_fields,
            current_total=current_quote.total_price,
            final_total=reservation.total_price,
        )
    return HostawayReservationCreateRequest(
        listing_map_id=listing_map_id,
        channel_id=channel_id,
        guest_first_name=intent.guest_first_name,
        guest_last_name=intent.guest_last_name,
        guest_email=intent.guest_email,
        guest_phone=intent.guest_phone,
        guest_country_code=intent.guest_country_code,
        guest_locale=intent.language,
        guests=reservation.guests,
        check_in=reservation.check_in,
        check_out=reservation.check_out,
        currency=reservation.currency,
        total_price=reservation.total_price,
        finance_fields=finance_fields,
        provider=settings.HOSTAWAY_RESERVATION_PROVIDER,
    )


def _override_price_details(
    *,
    finance_fields: tuple[ReservationFinanceField, ...],
    current_total: Decimal,
    final_total: Decimal,
) -> tuple[ReservationFinanceField, ...]:
    """Apply the owner's approved price to one included Hostaway rate field.

    Hostaway accepts explicit price details for API reservations.  We preserve
    every current component and put only the approved difference on an active
    included component, marking that one field as user-overridden.
    """

    difference = final_total - current_total
    preferred = sorted(
        enumerate(finance_fields),
        key=lambda item: 0 if item[1].name.casefold() == "baserate" else 1,
    )
    for index, field in preferred:
        if not field.is_included_in_total_price or field.is_deleted:
            continue
        adjusted_total = field.total + difference
        if adjusted_total < 0:
            continue
        quantity = field.quantity or 1
        adjusted_value = adjusted_total / Decimal(quantity)
        adjusted = replace(
            field,
            value=adjusted_value,
            total=adjusted_total,
            is_overridden_by_user=True,
        )
        return tuple(
            adjusted if candidate_index == index else candidate
            for candidate_index, candidate in enumerate(finance_fields)
        )
    raise ValueError("manual_price_cannot_be_applied")
