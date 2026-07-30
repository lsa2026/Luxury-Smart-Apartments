"""Build the minimal documented Hostaway reservation payload from trusted state."""

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
    if (
        current_quote.currency != reservation.currency
        or current_quote.total_price != reservation.total_price
    ):
        raise ValueError("revalidated_price_changed")
    finance_fields = tuple(
        ReservationFinanceField.from_price_component(component)
        for component in current_quote.components
    )
    return HostawayReservationCreateRequest(
        listing_map_id=listing_map_id,
        channel_id=channel_id,
        guest_first_name=intent.guest_first_name,
        guest_last_name=intent.guest_last_name,
        guest_email=intent.guest_email,
        guest_phone=intent.guest_phone,
        guest_country_code=intent.guest_country_code,
        guests=reservation.guests,
        check_in=reservation.check_in,
        check_out=reservation.check_out,
        currency=reservation.currency,
        total_price=reservation.total_price,
        finance_fields=finance_fields,
        provider=settings.HOSTAWAY_RESERVATION_PROVIDER,
    )
