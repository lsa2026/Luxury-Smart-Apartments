from datetime import timedelta
from decimal import Decimal
from io import StringIO

import pytest
from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.test import override_settings
from django.utils import timezone

from apps.integrations.hostaway.availability_validators import (
    PriceComponent,
    PriceQuote,
)
from apps.payments.models import PaymentAttempt
from apps.payments.providers import (
    DisabledPaymentProvider,
    PaymentProviderError,
    PaymentRequest,
)
from apps.properties.models import Property, PropertyImage
from apps.reservations.booking_forms import GuestDetailsForm
from apps.reservations.models import BookingIntent, BookingQuote
from apps.reservations.services.availability import AvailabilityResult
from apps.reservations.services.booking import (
    consume_revalidated_quote,
    create_quote_for_property,
)
from apps.reservations.signing import (
    quote_fingerprint,
    quote_reference,
    verify_quote_fingerprint,
)
from apps.reviews.models import Review

pytestmark = pytest.mark.django_db


def make_property() -> Property:
    return Property.objects.create(
        hostaway_listing_id=8001,
        slug="booking-property",
        name_ar="وحدة الحجز",
        person_capacity=4,
        currency_code="SAR",
        is_visible=True,
    )


def make_availability(
    property_obj: Property,
    *,
    total: Decimal = Decimal("500.25"),
    currency: str = "SAR",
    available: bool = True,
) -> AvailabilityResult:
    check_in = timezone.localdate() + timedelta(days=20)
    check_out = check_in + timedelta(days=2)
    price_quote = None
    if available:
        price_quote = PriceQuote(
            listing_id=property_obj.hostaway_listing_id,
            check_in=check_in,
            check_out=check_out,
            nights=2,
            guests=2,
            currency=currency,
            total_price=total,
            components=(
                PriceComponent(
                    listing_fee_setting_id=999,
                    type="accommodation",
                    name="baseRate",
                    title="Base rate",
                    alias="internal",
                    quantity=None,
                    value=total,
                    total=total,
                    is_included_in_total=True,
                ),
            ),
            calculated_at=timezone.now(),
            envelope_fields=frozenset(),
            result_field_types=(),
            component_field_types=(),
        )
    return AvailabilityResult(
        is_available=available,
        reason_code="available" if available else "unavailable_dates",
        user_message_ar="",
        user_message_en="",
        nights=2,
        quote=price_quote,
    )


def make_quote(
    property_obj: Property,
    *,
    total: Decimal = Decimal("500.25"),
    session_hash: str = "a" * 64,
) -> BookingQuote:
    return create_quote_for_property(
        make_availability(property_obj, total=total),
        property_obj=property_obj,
        session_hash=session_hash,
    )


def guest_data() -> dict[str, object]:
    return {
        "guest_first_name": "Test",
        "guest_last_name": "Guest",
        "guest_email": "test@example.invalid",
        "guest_phone": "+966500000000",
        "guest_country_code": "SA",
        "special_requests": "Synthetic request",
        "marketing_consent": False,
    }


def test_create_booking_quote_is_valid_sanitized_and_decimal() -> None:
    property_obj = make_property()
    quote = make_quote(property_obj)
    quote.refresh_from_db()

    assert quote.status == BookingQuote.Status.ACTIVE
    assert quote.total_price == Decimal("500.2500")
    assert quote.price_version == 2
    assert quote.hostaway_listing_id == property_obj.hostaway_listing_id
    assert quote.expires_at > quote.created_at
    assert quote.expires_at <= quote.created_at + timedelta(minutes=11)
    assert quote.components == [
        {
            "type": "accommodation",
            "title": "سعر الإقامة",
            "quantity": None,
            "value": "500.25",
            "included_in_total": True,
        }
    ]
    assert "listingFeeSettingId" not in str(quote.components)
    assert "internal" not in str(quote.components)


def test_quote_fingerprint_detects_tampering() -> None:
    quote = make_quote(make_property())
    assert verify_quote_fingerprint(quote) is True
    BookingQuote.objects.filter(pk=quote.pk).update(total_price=Decimal("1.00"))
    quote.refresh_from_db()
    assert verify_quote_fingerprint(quote) is False
    assert quote.signature != quote_fingerprint(quote)


def test_tampered_quote_cannot_be_consumed() -> None:
    property_obj = make_property()
    quote = make_quote(property_obj)
    BookingQuote.objects.filter(pk=quote.pk).update(total_price=Decimal("1.00"))
    outcome = consume_revalidated_quote(
        quote_id=quote.pk,
        session_hash=quote.session_key_hash,
        idempotency_key="i" * 32,
        guest_data=guest_data(),
        revalidated=make_availability(property_obj),
    )
    quote.refresh_from_db()
    assert outcome.code == "invalid_signature"
    assert quote.status == BookingQuote.Status.INVALIDATED
    assert BookingIntent.objects.count() == 0


def test_quote_reference_does_not_contain_price_or_listing_id() -> None:
    quote = make_quote(make_property())
    reference = quote_reference(quote)
    assert "500.25" not in reference
    assert "8001" not in reference


@pytest.mark.parametrize(
    "change",
    [
        {"check_out_offset": 0},
        {"nights": 3},
        {"guests": 5},
        {"hostaway_listing_id": 9999},
        {"currency": "INVALID"},
    ],
)
def test_quote_model_rules(change: dict[str, object]) -> None:
    property_obj = make_property()
    quote = make_quote(property_obj)
    if "check_out_offset" in change:
        quote.check_out = quote.check_in + timedelta(days=change["check_out_offset"])
    for field, value in change.items():
        if field != "check_out_offset":
            setattr(quote, field, value)
    with pytest.raises(ValidationError):
        quote.clean()


def test_guest_form_validation_normalization_and_xss() -> None:
    form = GuestDetailsForm(
        {
            "guest_first_name": "<b>Test</b>",
            "guest_last_name": "Guest",
            "guest_email": "test@example.invalid",
            "guest_phone": "+966 (50) 000-0000",
            "guest_country_code": "sa",
            "special_requests": "<script>alert(1)</script>Quiet room",
            "terms_accepted": "on",
            "privacy_accepted": "on",
            "idempotency_key": "x" * 32,
        }
    )
    assert form.is_valid(), form.errors
    assert form.cleaned_data["guest_first_name"] == "Test"
    assert form.cleaned_data["guest_phone"] == "+966500000000"
    assert form.cleaned_data["guest_country_code"] == "SA"
    assert "<script>" not in form.cleaned_data["special_requests"]
    assert form.cleaned_data["marketing_consent"] is False


@pytest.mark.parametrize(
    "overrides",
    [
        {"guest_email": "not-email"},
        {"guest_phone": "0500000000"},
        {"special_requests": "x" * 1001},
        {"guest_first_name": "<b></b>"},
        {"guest_last_name": "<i></i>"},
        {"terms_accepted": ""},
        {"privacy_accepted": ""},
    ],
)
def test_guest_form_rejects_invalid_input(overrides: dict[str, str]) -> None:
    data = {
        "guest_first_name": "Test",
        "guest_last_name": "Guest",
        "guest_email": "test@example.invalid",
        "guest_phone": "+966500000000",
        "guest_country_code": "SA",
        "special_requests": "",
        "terms_accepted": "on",
        "privacy_accepted": "on",
        "idempotency_key": "x" * 32,
    }
    data.update(overrides)
    assert GuestDetailsForm(data).is_valid() is False


def test_successful_revalidation_consumes_quote_once_without_payment() -> None:
    property_obj = make_property()
    quote = make_quote(property_obj)
    before_business = (
        Property.objects.count(),
        PropertyImage.objects.count(),
        Review.objects.count(),
    )
    outcome = consume_revalidated_quote(
        quote_id=quote.pk,
        session_hash=quote.session_key_hash,
        idempotency_key="i" * 32,
        guest_data=guest_data(),
        revalidated=make_availability(property_obj),
    )
    quote.refresh_from_db()

    assert outcome.code == "created"
    assert outcome.intent is not None
    assert outcome.intent.status == BookingIntent.Status.AWAITING_PAYMENT
    assert quote.status == BookingQuote.Status.CONSUMED
    assert BookingIntent.objects.count() == 1
    assert PaymentAttempt.objects.count() == 0
    assert (
        Property.objects.count(),
        PropertyImage.objects.count(),
        Review.objects.count(),
    ) == before_business


def test_idempotency_prevents_duplicate_intent_and_double_consumption() -> None:
    property_obj = make_property()
    quote = make_quote(property_obj)
    arguments = {
        "quote_id": quote.pk,
        "session_hash": quote.session_key_hash,
        "idempotency_key": "i" * 32,
        "guest_data": guest_data(),
        "revalidated": make_availability(property_obj),
    }
    first = consume_revalidated_quote(**arguments)
    second = consume_revalidated_quote(**arguments)
    duplicate = consume_revalidated_quote(**{**arguments, "idempotency_key": "j" * 32})
    assert first.intent == second.intent
    assert second.code == "idempotent"
    assert duplicate.intent is None
    assert BookingIntent.objects.count() == 1


@pytest.mark.parametrize(
    ("total", "currency"),
    [
        (Decimal("501.00"), "SAR"),
        (Decimal("500.25"), "USD"),
    ],
)
def test_price_or_currency_change_creates_replacement_quote_only(
    total: Decimal,
    currency: str,
) -> None:
    property_obj = make_property()
    quote = make_quote(property_obj)
    outcome = consume_revalidated_quote(
        quote_id=quote.pk,
        session_hash=quote.session_key_hash,
        idempotency_key="i" * 32,
        guest_data=guest_data(),
        revalidated=make_availability(
            property_obj,
            total=total,
            currency=currency,
        ),
    )
    quote.refresh_from_db()
    assert outcome.code == "price_changed"
    assert outcome.replacement_quote is not None
    assert quote.status == BookingQuote.Status.PRICE_CHANGED
    assert BookingIntent.objects.count() == 0


def test_unavailable_revalidation_invalidates_quote_without_intent() -> None:
    property_obj = make_property()
    quote = make_quote(property_obj)
    outcome = consume_revalidated_quote(
        quote_id=quote.pk,
        session_hash=quote.session_key_hash,
        idempotency_key="i" * 32,
        guest_data=guest_data(),
        revalidated=make_availability(property_obj, available=False),
    )
    quote.refresh_from_db()
    assert outcome.code == "unavailable"
    assert quote.status == BookingQuote.Status.UNAVAILABLE
    assert BookingIntent.objects.count() == 0


def test_expired_and_consumed_quotes_cannot_be_reused() -> None:
    property_obj = make_property()
    quote = make_quote(property_obj)
    BookingQuote.objects.filter(pk=quote.pk).update(
        expires_at=timezone.now() - timedelta(seconds=1)
    )
    expired = consume_revalidated_quote(
        quote_id=quote.pk,
        session_hash=quote.session_key_hash,
        idempotency_key="i" * 32,
        guest_data=guest_data(),
        revalidated=make_availability(property_obj),
    )
    assert expired.code == "quote_expired"


def test_session_mismatch_is_generic_and_creates_nothing() -> None:
    property_obj = make_property()
    quote = make_quote(property_obj)
    outcome = consume_revalidated_quote(
        quote_id=quote.pk,
        session_hash="b" * 64,
        idempotency_key="i" * 32,
        guest_data=guest_data(),
        revalidated=make_availability(property_obj),
    )
    assert outcome.code == "not_found"
    assert BookingIntent.objects.count() == 0


def test_public_references_are_unique_and_nonsequential() -> None:
    property_obj = make_property()
    quote_one = make_quote(property_obj)
    first = consume_revalidated_quote(
        quote_id=quote_one.pk,
        session_hash=quote_one.session_key_hash,
        idempotency_key="i" * 32,
        guest_data=guest_data(),
        revalidated=make_availability(property_obj),
    ).intent
    quote_two = make_quote(property_obj)
    second = consume_revalidated_quote(
        quote_id=quote_two.pk,
        session_hash=quote_two.session_key_hash,
        idempotency_key="j" * 32,
        guest_data=guest_data(),
        revalidated=make_availability(property_obj),
    ).intent
    assert first is not None and second is not None
    assert first.public_reference != second.public_reference
    assert len(first.public_reference) >= 20


def test_disabled_payment_provider_never_creates_attempt() -> None:
    provider = DisabledPaymentProvider()
    with pytest.raises(PaymentProviderError) as exc_info:
        provider.create_session(
            PaymentRequest(
                booking_reference="synthetic",
                amount=Decimal("10.00"),
                currency="SAR",
                return_url="https://example.invalid/return",
            )
        )
    assert exc_info.value.code == "payment_provider_not_configured"
    assert PaymentAttempt.objects.count() == 0


@override_settings(BOOKING_QUOTE_TTL_SECONDS=600)
def test_expire_command_and_dry_run() -> None:
    property_obj = make_property()
    quote = make_quote(property_obj)
    BookingQuote.objects.filter(pk=quote.pk).update(
        expires_at=timezone.now() - timedelta(seconds=1)
    )
    dry_output = StringIO()
    call_command("expire_booking_intents", dry_run=True, stdout=dry_output)
    quote.refresh_from_db()
    assert quote.status == BookingQuote.Status.ACTIVE
    assert "would expire: 1" in dry_output.getvalue()

    output = StringIO()
    call_command("expire_booking_intents", stdout=output)
    quote.refresh_from_db()
    assert quote.status == BookingQuote.Status.EXPIRED
    assert "Hostaway changes: 0" in output.getvalue()


def test_expire_command_expires_incomplete_intent_without_external_changes() -> None:
    property_obj = make_property()
    quote = make_quote(property_obj)
    intent = consume_revalidated_quote(
        quote_id=quote.pk,
        session_hash=quote.session_key_hash,
        idempotency_key="i" * 32,
        guest_data=guest_data(),
        revalidated=make_availability(property_obj),
    ).intent
    assert intent is not None
    BookingIntent.objects.filter(pk=intent.pk).update(
        expires_at=timezone.now() - timedelta(seconds=1)
    )

    output = StringIO()
    call_command("expire_booking_intents", stdout=output)
    intent.refresh_from_db()
    assert intent.status == BookingIntent.Status.EXPIRED
    assert "Booking intents expired: 1" in output.getvalue()
    assert "Hostaway changes: 0" in output.getvalue()
    assert "Payment changes: 0" in output.getvalue()
