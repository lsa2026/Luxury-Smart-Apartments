from dataclasses import replace
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
                    is_deleted=False,
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
        "billing_street1": "King Fahd Road 10",
        "billing_city": "Riyadh",
        "billing_state": "Riyadh",
        "billing_country": "SA",
        "billing_postcode": "12345",
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
            "total": "500.25",
            "included_in_total": True,
        }
    ]
    assert "listingFeeSettingId" not in str(quote.components)
    assert "internal" not in str(quote.components)


def test_fx_payment_fields_are_covered_by_quote_hmac() -> None:
    quote = make_quote(make_property())
    assert verify_quote_fingerprint(quote) is True
    quote.payment_amount_sar = Decimal("0.01")
    assert verify_quote_fingerprint(quote) is False


def test_quote_breakdown_uses_component_total_not_unit_value() -> None:
    property_obj = make_property()
    availability = make_availability(property_obj, total=Decimal("30.00"))
    assert availability.quote is not None
    component = replace(
        availability.quote.components[0],
        quantity=3,
        value=Decimal("10.00"),
        total=Decimal("30.00"),
    )
    availability = replace(
        availability,
        quote=replace(availability.quote, components=(component,)),
    )

    quote = create_quote_for_property(
        availability,
        property_obj=property_obj,
        session_hash="a" * 64,
    )

    assert quote.components[0]["total"] == "30.00"
    assert "value" not in quote.components[0]


def test_quote_breakdown_excludes_optional_and_deleted_components() -> None:
    property_obj = make_property()
    availability = make_availability(property_obj, total=Decimal("30.00"))
    assert availability.quote is not None
    base = replace(
        availability.quote.components[0],
        value=Decimal("30.00"),
        total=Decimal("30.00"),
    )
    optional = replace(
        base,
        name="parkingFee",
        title="Parking fee",
        value=Decimal("5.00"),
        total=Decimal("5.00"),
        is_included_in_total=False,
    )
    deleted = replace(
        base,
        name="oldFee",
        title="Old fee",
        value=Decimal("7.00"),
        total=Decimal("7.00"),
        is_deleted=True,
    )
    availability = replace(
        availability,
        quote=replace(availability.quote, components=(base, optional, deleted)),
    )

    quote = create_quote_for_property(
        availability,
        property_obj=property_obj,
        session_hash="a" * 64,
    )

    assert len(quote.components) == 1
    assert quote.components[0]["title"] == "سعر الإقامة"
    assert quote.components[0]["total"] == "30.00"


def test_quote_breakdown_is_hidden_when_components_do_not_match_total() -> None:
    property_obj = make_property()
    availability = make_availability(property_obj, total=Decimal("30.00"))
    assert availability.quote is not None
    mismatched = replace(
        availability.quote.components[0],
        value=Decimal("29.00"),
        total=Decimal("29.00"),
    )
    availability = replace(
        availability,
        quote=replace(availability.quote, components=(mismatched,)),
    )

    quote = create_quote_for_property(
        availability,
        property_obj=property_obj,
        session_hash="a" * 64,
    )

    assert quote.total_price == Decimal("30.0000")
    assert quote.components == []


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
            "guest_phone": "050 000 0000",
            "billing_street1": "King Fahd Road 10",
            "billing_city": "Riyadh",
            "billing_state": "Riyadh",
            "billing_country": "SA",
            "billing_postcode": "12345",
            "special_requests": "<script>alert(1)</script>Quiet room",
            "terms_accepted": "on",
            "privacy_accepted": "on",
            "idempotency_key": "x" * 32,
        }
    )
    assert form.is_valid(), form.errors
    assert form.cleaned_data["guest_first_name"] == "Test"
    assert form.cleaned_data["guest_phone"] == "+966500000000"
    assert "guest_country_code" not in form.fields
    assert "<script>" not in form.cleaned_data["special_requests"]
    assert form.cleaned_data["marketing_consent"] is False


def test_guest_form_uses_the_stay_country_for_a_local_mobile_number() -> None:
    form = GuestDetailsForm(
        {
            "guest_first_name": "Test",
            "guest_last_name": "Guest",
            "guest_email": "test@example.invalid",
            "guest_phone": "0612345678",
            "billing_street1": "Avenue Hassan II 10",
            "billing_city": "Marrakech",
            "billing_state": "Marrakech-Safi",
            "billing_country": "MA",
            "billing_postcode": "40000",
            "special_requests": "",
            "terms_accepted": "on",
            "privacy_accepted": "on",
            "idempotency_key": "x" * 32,
        },
        default_country_code="MA",
    )

    assert form.is_valid(), form.errors
    assert form.cleaned_data["guest_phone"] == "+212612345678"


def test_guest_phone_uses_selected_billing_country_not_property_country() -> None:
    form = GuestDetailsForm(
        {
            "guest_first_name": "Test",
            "guest_last_name": "Guest",
            "guest_email": "test@example.invalid",
            "guest_phone": "0500000000",
            "billing_street1": "King Fahd Road 10",
            "billing_city": "Riyadh",
            "billing_state": "Riyadh",
            "billing_country": "SA",
            "billing_postcode": "12345",
            "special_requests": "",
            "terms_accepted": "on",
            "privacy_accepted": "on",
            "idempotency_key": "x" * 32,
        },
        default_country_code="MA",
    )
    assert form.is_valid(), form.errors
    assert form.cleaned_data["guest_phone"] == "+966500000000"
    assert form.fields["billing_country"].widget.input_type == "select"
    assert form.fields["billing_country"].widget.attrs["data-country-select"] == ""


@pytest.mark.parametrize(
    ("raw_phone", "billing_country", "expected"),
    [
        # A local number written the way each country writes it.
        ("01012345678", "EG", "+201012345678"),
        ("07911123456", "GB", "+447911123456"),
        ("(212) 555-1234", "US", "+12125551234"),
        ("98765 43210", "IN", "+919876543210"),
        ("0151 12345678", "DE", "+4915112345678"),
        ("090-1234-5678", "JP", "+819012345678"),
        # An explicit country code is authoritative wherever it is typed.
        ("+33612345678", "GB", "+33612345678"),
        ("0033612345678", "GB", "+33612345678"),
        # A guest billing overseas may still carry a Saudi mobile: the number
        # falls back to the site default rather than being rejected.
        ("+966500000000", "GB", "+966500000000"),
    ],
)
def test_guest_phone_accepts_numbers_from_any_country(
    raw_phone: str,
    billing_country: str,
    expected: str,
) -> None:
    form = GuestDetailsForm(
        {
            "guest_first_name": "Test",
            "guest_last_name": "Guest",
            "guest_email": "test@example.invalid",
            "guest_phone": raw_phone,
            "billing_street1": "1 Example Street",
            "billing_city": "Example City",
            "billing_state": "Example Region",
            "billing_country": billing_country,
            "billing_postcode": "12345",
            "special_requests": "",
            "terms_accepted": "on",
            "privacy_accepted": "on",
            "idempotency_key": "x" * 32,
        },
    )

    assert form.is_valid(), form.errors
    assert form.cleaned_data["guest_phone"] == expected


@pytest.mark.parametrize(
    "raw_phone",
    [
        "12345",  # too short for any country
        "+9999999999999999",  # no such country code, and over E.164 length
        "0000000000",
        "abcdefghij",
    ],
)
def test_guest_phone_still_rejects_numbers_that_are_not_dialable(raw_phone: str) -> None:
    form = GuestDetailsForm(
        {
            "guest_first_name": "Test",
            "guest_last_name": "Guest",
            "guest_email": "test@example.invalid",
            "guest_phone": raw_phone,
            "billing_street1": "King Fahd Road 10",
            "billing_city": "Riyadh",
            "billing_state": "Riyadh",
            "billing_country": "SA",
            "billing_postcode": "12345",
            "special_requests": "",
            "terms_accepted": "on",
            "privacy_accepted": "on",
            "idempotency_key": "x" * 32,
        },
    )

    assert form.is_valid() is False
    assert "guest_phone" in form.errors


def test_guest_form_rejects_unknown_billing_country_choice() -> None:
    data = {
        "guest_first_name": "Test",
        "guest_last_name": "Guest",
        "guest_email": "test@example.invalid",
        "guest_phone": "+966500000000",
        "billing_street1": "King Fahd Road 10",
        "billing_city": "Riyadh",
        "billing_state": "Riyadh",
        "billing_country": "XX",
        "billing_postcode": "12345",
        "special_requests": "",
        "terms_accepted": "on",
        "privacy_accepted": "on",
        "idempotency_key": "x" * 32,
    }
    assert GuestDetailsForm(data).is_valid() is False


@pytest.mark.parametrize(
    "overrides",
    [
        {"guest_email": "not-email"},
        {"guest_phone": "12345"},
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
        "guest_phone": "0500000000",
        "billing_street1": "King Fahd Road 10",
        "billing_city": "Riyadh",
        "billing_state": "Riyadh",
        "billing_country": "SA",
        "billing_postcode": "12345",
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
