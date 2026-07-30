"""Persist quotes and consume them into local, unconfirmed booking intents."""

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from django.conf import settings
from django.db import IntegrityError, transaction
from django.utils import timezone
from django.utils.crypto import constant_time_compare

from apps.integrations.hostaway.availability_validators import PriceQuote

from ..models import BookingIntent, BookingQuote
from ..signing import quote_fingerprint, verify_quote_fingerprint
from .availability import AvailabilityResult, component_title_ar


@dataclass(frozen=True, slots=True)
class QuoteCreation:
    availability: AvailabilityResult
    quote: BookingQuote | None


@dataclass(frozen=True, slots=True)
class IntentCreation:
    code: str
    intent: BookingIntent | None = None
    replacement_quote: BookingQuote | None = None
    old_total: Decimal | None = None
    new_total: Decimal | None = None


def sanitized_components(price_quote: PriceQuote) -> list[dict[str, Any]]:
    components: list[dict[str, Any]] = []
    for component in price_quote.components:
        components.append(
            {
                "type": component.type[:50],
                "title": component_title_ar(component)[:200],
                "quantity": component.quantity,
                "value": format(component.value, "f"),
                "included_in_total": component.is_included_in_total,
            }
        )
    return components


def create_quote_for_property(
    availability: AvailabilityResult,
    *,
    property_obj: Any,
    session_hash: str,
) -> BookingQuote:
    price_quote = availability.quote
    if not availability.is_available or price_quote is None:
        raise ValueError("A quote requires verified availability and price.")
    if price_quote.listing_id != property_obj.hostaway_listing_id:
        raise ValueError("Quote listing does not match the local property.")
    if price_quote.total_price < 0 or price_quote.total_price.as_tuple().exponent < -4:
        raise ValueError("Price total has an unsupported precision.")
    components = sanitized_components(price_quote)
    if len(components) > 100:
        raise ValueError("Price component count exceeds the safe limit.")

    quote = BookingQuote(
        property=property_obj,
        hostaway_listing_id=property_obj.hostaway_listing_id,
        check_in=price_quote.check_in,
        check_out=price_quote.check_out,
        nights=price_quote.nights,
        guests=price_quote.guests,
        currency=price_quote.currency,
        total_price=price_quote.total_price,
        components=components,
        price_version=2,
        session_key_hash=session_hash,
        expires_at=BookingQuote.default_expiry(),
        calculated_at=price_quote.calculated_at,
    )
    quote.signature = quote_fingerprint(quote)
    quote.clean()
    with transaction.atomic():
        quote.save(force_insert=True)
    return quote


def consume_revalidated_quote(
    *,
    quote_id: object,
    session_hash: str,
    idempotency_key: str,
    guest_data: dict[str, Any],
    revalidated: AvailabilityResult,
) -> IntentCreation:
    """Consume a quote after network revalidation has completed outside this function."""
    existing = (
        BookingIntent.objects.select_related("quote")
        .filter(idempotency_key=idempotency_key)
        .first()
    )
    if existing is not None:
        if existing.quote_id == quote_id and constant_time_compare(
            existing.session_key_hash,
            session_hash,
        ):
            return IntentCreation("idempotent", intent=existing)
        return IntentCreation("idempotency_conflict")

    with transaction.atomic():
        quote = BookingQuote.objects.select_for_update().select_related("property").get(pk=quote_id)
        if not constant_time_compare(quote.session_key_hash, session_hash):
            return IntentCreation("not_found")
        if not verify_quote_fingerprint(quote):
            quote.status = BookingQuote.Status.INVALIDATED
            quote.invalidated_at = timezone.now()
            quote.save(update_fields=["status", "invalidated_at", "updated_at"])
            return IntentCreation("invalid_signature")
        if quote.status != BookingQuote.Status.ACTIVE:
            existing_for_quote = BookingIntent.objects.filter(quote=quote).first()
            if (
                existing_for_quote
                and existing_for_quote.idempotency_key == idempotency_key
                and constant_time_compare(
                    existing_for_quote.session_key_hash,
                    session_hash,
                )
            ):
                return IntentCreation("idempotent", intent=existing_for_quote)
            return IntentCreation("quote_not_active")
        if quote.is_expired:
            quote.status = BookingQuote.Status.EXPIRED
            quote.save(update_fields=["status", "updated_at"])
            return IntentCreation("quote_expired")
        if not revalidated.is_available or revalidated.quote is None:
            quote.status = BookingQuote.Status.UNAVAILABLE
            quote.invalidated_at = timezone.now()
            quote.save(update_fields=["status", "invalidated_at", "updated_at"])
            from apps.notifications.services.events import dispatch_event

            transaction.on_commit(
                lambda: dispatch_event(
                    "booking_intent.unavailable",
                    event_key=f"quote-unavailable:{quote.pk}",
                    related_object_type="BookingQuote",
                    related_object_reference=str(quote.pk),
                    action_url=f"/admin/reservations/bookingquote/{quote.pk}/change/",
                )
            )
            return IntentCreation("unavailable")

        latest = revalidated.quote
        if (
            latest.listing_id != quote.hostaway_listing_id
            or latest.check_in != quote.check_in
            or latest.check_out != quote.check_out
            or latest.guests != quote.guests
        ):
            quote.status = BookingQuote.Status.INVALIDATED
            quote.invalidated_at = timezone.now()
            quote.save(update_fields=["status", "invalidated_at", "updated_at"])
            return IntentCreation("revalidation_mismatch")
        price_changed = (
            latest.currency != quote.currency
            or abs(latest.total_price - quote.total_price) > settings.BOOKING_PRICE_TOLERANCE
        )
        if price_changed:
            quote.status = BookingQuote.Status.PRICE_CHANGED
            quote.invalidated_at = timezone.now()
            quote.save(update_fields=["status", "invalidated_at", "updated_at"])
            replacement = create_quote_for_property(
                revalidated,
                property_obj=quote.property,
                session_hash=session_hash,
            )
            from apps.notifications.services.events import dispatch_event

            transaction.on_commit(
                lambda: dispatch_event(
                    "booking_intent.price_changed",
                    event_key=f"quote-price-changed:{quote.pk}",
                    related_object_type="BookingQuote",
                    related_object_reference=str(quote.pk),
                    action_url=f"/admin/reservations/bookingquote/{quote.pk}/change/",
                )
            )
            return IntentCreation(
                "price_changed",
                replacement_quote=replacement,
                old_total=quote.total_price,
                new_total=latest.total_price,
            )

        now = timezone.now()
        intent = BookingIntent(
            quote=quote,
            property=quote.property,
            check_in=quote.check_in,
            check_out=quote.check_out,
            nights=quote.nights,
            guests=quote.guests,
            currency=quote.currency,
            total_price=quote.total_price,
            guest_first_name=guest_data["guest_first_name"],
            guest_last_name=guest_data["guest_last_name"],
            guest_email=guest_data["guest_email"],
            guest_phone=guest_data["guest_phone"],
            guest_country_code=guest_data["guest_country_code"],
            special_requests=guest_data.get("special_requests", ""),
            status=BookingIntent.Status.AWAITING_PAYMENT,
            idempotency_key=idempotency_key,
            session_key_hash=session_hash,
            terms_accepted_at=now,
            privacy_accepted_at=now,
            marketing_consent=guest_data.get("marketing_consent", False),
            expires_at=BookingIntent.default_expiry(),
        )
        intent.full_clean(validate_unique=False, validate_constraints=False)
        try:
            with transaction.atomic():
                intent.save(force_insert=True)
        except IntegrityError:
            existing_intent = BookingIntent.objects.filter(quote=quote).first()
            if (
                existing_intent
                and existing_intent.idempotency_key == idempotency_key
                and constant_time_compare(
                    existing_intent.session_key_hash,
                    session_hash,
                )
            ):
                return IntentCreation("idempotent", intent=existing_intent)
            return IntentCreation("duplicate")
        quote.status = BookingQuote.Status.CONSUMED
        quote.consumed_at = now
        quote.save(update_fields=["status", "consumed_at", "updated_at"])
        from apps.notifications.services.events import handle_booking_intent_created

        transaction.on_commit(lambda: handle_booking_intent_created(intent.pk))
        return IntentCreation("created", intent=intent)
