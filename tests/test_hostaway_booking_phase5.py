from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
from io import StringIO
from unittest.mock import Mock, patch

import httpx
import pytest
from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.db import connection
from django.test import override_settings
from django.utils import timezone

from apps.integrations.hostaway.client import HostawayClient
from apps.integrations.hostaway.exceptions import (
    HostawayAuthenticationError,
    HostawayConfigurationError,
    HostawayTimeoutError,
)
from apps.integrations.hostaway.reservation_validators import (
    HOSTAWAY_RESERVATION_STATUS_MAP,
    HostawayReservationCreateRequest,
    HostawayReservationCreateResult,
    HostawayReservationSnapshot,
    ReservationFinanceField,
    normalize_hostaway_reservation_status,
)
from apps.integrations.tasks import reconcile_paid_hostaway_reservations_task
from apps.payments.models import PaymentAttempt
from apps.reservations.models import (
    BookingIntent,
    HostawayReservationOperation,
    Reservation,
)
from apps.reservations.services.booking import consume_revalidated_quote
from apps.reservations.services.hostaway_booking import (
    HostawayBookingService,
    prepare_local_reservation,
)
from apps.reservations.services.reservation_payloads import (
    build_hostaway_reservation_request,
)
from tests.test_booking_models_services import (
    guest_data,
    make_availability,
    make_property,
    make_quote,
)

pytestmark = pytest.mark.django_db


def make_intent(
    *,
    listing_map_id: int | None = 9001,
    property_obj=None,
) -> BookingIntent:
    property_obj = property_obj or make_property()
    property_obj.hostaway_listing_map_id = listing_map_id
    property_obj.save(update_fields=["hostaway_listing_map_id"])
    quote = make_quote(property_obj)
    intent = consume_revalidated_quote(
        quote_id=quote.pk,
        session_hash=quote.session_key_hash,
        idempotency_key=f"phase5-intent-key-{BookingIntent.objects.count():032d}",
        guest_data=guest_data(),
        revalidated=make_availability(property_obj),
    ).intent
    assert intent is not None
    return intent


def complete_availability(intent: BookingIntent, *, total: Decimal | None = None):
    availability = make_availability(
        intent.property,
        total=total or intent.total_price,
        currency=intent.currency,
    )
    assert availability.quote is not None
    component = replace(
        availability.quote.components[0],
        total=availability.quote.components[0].value,
        is_included_in_total=True,
        is_overridden_by_user=False,
        is_mandatory=True,
        is_deleted=False,
    )
    return replace(
        availability,
        quote=replace(availability.quote, components=(component,)),
    )


def successful_payment(intent: BookingIntent) -> PaymentAttempt:
    return PaymentAttempt.objects.create(
        booking_intent=intent,
        provider="synthetic",
        amount=intent.total_price,
        currency=intent.currency,
        status=PaymentAttempt.Status.SUCCEEDED,
        idempotency_key="phase5-payment-key-00000000000",
    )


@override_settings(HOSTAWAY_LIVE_BOOKING_ENABLED=True)
def test_recovery_task_replays_a_verified_paid_booking() -> None:
    intent = make_intent()
    reservation = prepare_local_reservation(intent)
    successful_payment(intent)
    reservation.normalized_status = Reservation.Status.READY_FOR_HOSTAWAY
    reservation.payment_status = "paid"
    reservation.save(update_fields=["normalized_status", "payment_status", "updated_at"])
    service = Mock()
    service.create_hostaway_reservation.return_value = Mock(code="confirmed")
    manager = Mock()
    manager.__enter__ = Mock(return_value=service)
    manager.__exit__ = Mock(return_value=None)

    with (
        patch("apps.integrations.tasks.call_command"),
        patch(
            "apps.reservations.services.hostaway_booking.HostawayBookingService",
            return_value=manager,
        ),
    ):
        result = reconcile_paid_hostaway_reservations_task.run()

    service.create_hostaway_reservation.assert_called_once_with(reservation)
    assert result["processed"] == 1
    assert result["confirmed"] == 1


@override_settings(HOSTAWAY_LIVE_BOOKING_ENABLED=True)
def test_recovery_task_replays_pre_post_failure_but_not_a_sent_failure() -> None:
    replay_intent = make_intent()
    replay = prepare_local_reservation(replay_intent)
    successful_payment(replay_intent)
    replay.normalized_status = Reservation.Status.CREATE_FAILED
    replay.payment_status = "paid"
    replay.save(update_fields=["normalized_status", "payment_status", "updated_at"])
    HostawayReservationOperation.objects.create(
        reservation=replay,
        operation_type=HostawayReservationOperation.OperationType.CREATE_RESERVATION,
        idempotency_key="phase5-replay-blocked-operation",
        request_fingerprint="a" * 64,
        status=HostawayReservationOperation.Status.BLOCKED,
        attempt_count=0,
        error_code="price_component_flags_missing",
    )

    no_replay_intent = make_intent(property_obj=replay.property)
    no_replay = prepare_local_reservation(no_replay_intent)
    PaymentAttempt.objects.create(
        booking_intent=no_replay_intent,
        provider="synthetic",
        amount=no_replay_intent.total_price,
        currency=no_replay_intent.currency,
        status=PaymentAttempt.Status.SUCCEEDED,
        idempotency_key="phase5-no-replay-payment-key",
    )
    no_replay.normalized_status = Reservation.Status.CREATE_FAILED
    no_replay.payment_status = "paid"
    no_replay.save(update_fields=["normalized_status", "payment_status", "updated_at"])
    HostawayReservationOperation.objects.create(
        reservation=no_replay,
        operation_type=HostawayReservationOperation.OperationType.CREATE_RESERVATION,
        idempotency_key="phase5-no-replay-failed-operation",
        request_fingerprint="b" * 64,
        status=HostawayReservationOperation.Status.FAILED,
        attempt_count=1,
        error_code="hostaway_create_rejected",
    )

    service = Mock()
    service.create_hostaway_reservation.return_value = Mock(code="confirmed")
    manager = Mock()
    manager.__enter__ = Mock(return_value=service)
    manager.__exit__ = Mock(return_value=None)
    with (
        patch("apps.integrations.tasks.call_command"),
        patch(
            "apps.reservations.services.hostaway_booking.HostawayBookingService",
            return_value=manager,
        ),
    ):
        result = reconcile_paid_hostaway_reservations_task.run()

    service.create_hostaway_reservation.assert_called_once_with(replay)
    assert result["processed"] == 1
    assert result["confirmed"] == 1


class AvailabilityStub:
    def __init__(self, result: object) -> None:
        self.result = result
        self.calls = 0

    def check(self, request: object, *, bypass_cache: bool) -> object:
        assert bypass_cache is True
        self.calls += 1
        return self.result

    def close(self) -> None:
        pass


class CreateClientStub:
    def __init__(
        self,
        result: HostawayReservationCreateResult | None = None,
        error: Exception | None = None,
    ) -> None:
        self.result = result
        self.error = error
        self.calls = 0
        self.in_atomic_during_call: bool | None = None
        self.savepoint_depth: int | None = None

    def create_reservation_with_price_details(self, request: object) -> object:
        self.calls += 1
        self.in_atomic_during_call = connection.in_atomic_block
        self.savepoint_depth = len(connection.savepoint_ids)
        if self.error:
            raise self.error
        return self.result

    def close(self) -> None:
        pass


def create_result(reservation: Reservation) -> HostawayReservationCreateResult:
    return HostawayReservationCreateResult(
        HostawayReservationSnapshot(
            reservation_id=77001,
            listing_map_id=reservation.property.hostaway_listing_map_id,
            channel_id=2000,
            status="new",
            check_in=reservation.check_in,
            check_out=reservation.check_out,
            guests=reservation.guests,
            currency=reservation.currency,
            total_price=reservation.total_price,
            payment_status="paid",
            source="LuxurySmartApartments",
            updated_at=timezone.now(),
        )
    )


def test_prepare_local_reservation_is_idempotent_and_sanitized() -> None:
    intent = make_intent()
    first = prepare_local_reservation(intent)
    second = prepare_local_reservation(intent)
    assert first.pk == second.pk
    assert Reservation.objects.count() == 1
    assert first.booking_intent == intent
    assert first.nights == (first.check_out - first.check_in).days
    assert first.total_price == Decimal("500.2500")
    assert first.public_reference != str(first.pk)
    field_names = {field.name for field in Reservation._meta.fields}
    assert not {"guest_email", "guest_phone", "door_code", "host_note"} & field_names


def test_prepare_requires_awaiting_payment_intent() -> None:
    intent = make_intent()
    intent.status = BookingIntent.Status.EXPIRED
    intent.save(update_fields=["status"])
    with pytest.raises(ValueError, match="booking_intent_not_awaiting_payment"):
        prepare_local_reservation(intent)


def test_reservation_validation_and_confirmed_constraint() -> None:
    reservation = prepare_local_reservation(make_intent())
    reservation.normalized_status = Reservation.Status.CONFIRMED
    with pytest.raises(ValidationError):
        reservation.full_clean()
    reservation.normalized_status = Reservation.Status.AWAITING_PAYMENT
    reservation.nights += 1
    with pytest.raises(ValidationError):
        reservation.full_clean()


def test_public_references_are_nonsequential() -> None:
    first = prepare_local_reservation(make_intent())
    second_intent = make_intent(property_obj=first.property)
    second = prepare_local_reservation(second_intent)
    assert first.public_reference != second.public_reference
    assert len(first.public_reference) >= 20


@override_settings(HOSTAWAY_LIVE_BOOKING_ENABLED=False)
def test_feature_flag_blocks_before_availability_or_post() -> None:
    reservation = prepare_local_reservation(make_intent())
    availability = AvailabilityStub(complete_availability(reservation.booking_intent))
    client = CreateClientStub()
    outcome = HostawayBookingService(
        client=client,
        availability_service=availability,
    ).create_hostaway_reservation(reservation)
    assert outcome.code == "hostaway_live_booking_disabled"
    assert availability.calls == 0
    assert client.calls == 0
    assert reservation.booking_intent.status == BookingIntent.Status.AWAITING_PAYMENT


@override_settings(
    HOSTAWAY_LIVE_BOOKING_ENABLED=True,
    HOSTAWAY_DIRECT_CHANNEL_ID=2000,
)
def test_successful_payment_is_required() -> None:
    reservation = prepare_local_reservation(make_intent())
    availability = AvailabilityStub(complete_availability(reservation.booking_intent))
    client = CreateClientStub()
    outcome = HostawayBookingService(
        client=client,
        availability_service=availability,
    ).create_hostaway_reservation(reservation)
    assert outcome.code == "successful_payment_required"
    assert client.calls == 0
    assert PaymentAttempt.objects.count() == 0


@pytest.mark.parametrize(
    ("listing_map_id", "channel_id", "expected"),
    [
        (None, 2000, "listing_map_id_not_verified"),
        (9001, None, "direct_channel_id_not_configured"),
    ],
)
@override_settings(HOSTAWAY_LIVE_BOOKING_ENABLED=True)
def test_verified_map_and_channel_are_required(
    listing_map_id: int | None,
    channel_id: int | None,
    expected: str,
) -> None:
    intent = make_intent(listing_map_id=listing_map_id)
    reservation = prepare_local_reservation(intent)
    successful_payment(intent)
    availability = AvailabilityStub(complete_availability(intent))
    client = CreateClientStub()
    with override_settings(HOSTAWAY_DIRECT_CHANNEL_ID=channel_id):
        outcome = HostawayBookingService(
            client=client,
            availability_service=availability,
        ).create_hostaway_reservation(reservation)
    assert outcome.code == expected
    assert client.calls == 0
    if listing_map_id is None:
        assert reservation.hostaway_listing_id != reservation.hostaway_listing_map_id


@override_settings(HOSTAWAY_DIRECT_CHANNEL_ID=2000)
def test_payload_is_documented_allowlist_and_uses_map_id() -> None:
    intent = make_intent(listing_map_id=9001)
    reservation = prepare_local_reservation(intent)
    request = build_hostaway_reservation_request(
        reservation,
        current_quote=complete_availability(intent).quote,
    )
    payload = request.to_payload()
    assert payload["listingMapId"] == 9001
    assert payload["listingMapId"] != reservation.hostaway_listing_id
    assert set(payload) == {
        "channelId",
        "listingMapId",
        "isManuallyChecked",
        "isInitial",
        "guestName",
        "guestFirstName",
        "guestLastName",
        "guestCountry",
        "guestEmail",
        "phone",
        "numberOfGuests",
        "adults",
        "arrivalDate",
        "departureDate",
        "totalPrice",
        "currency",
        "financeField",
    }
    serialized = str(payload)
    assert "forceOverbooking" not in serialized
    assert "validatePaymentMethod" not in serialized
    assert "ccNumber" not in serialized
    assert "cvc" not in serialized
    assert "doorCode" not in serialized
    assert "guestNote" not in serialized
    assert payload["financeField"][0]["isMandatory"] == 1


@override_settings(HOSTAWAY_DIRECT_CHANNEL_ID=2000)
def test_payload_preserves_null_is_mandatory_from_hostaway_quote() -> None:
    intent = make_intent(listing_map_id=9001)
    reservation = prepare_local_reservation(intent)
    availability = complete_availability(intent)
    assert availability.quote is not None
    component = replace(availability.quote.components[0], is_mandatory=None)

    request = build_hostaway_reservation_request(
        reservation,
        current_quote=replace(availability.quote, components=(component,)),
    )

    assert request.to_payload()["financeField"][0]["isMandatory"] is None


@override_settings(
    HOSTAWAY_LIVE_BOOKING_ENABLED=True,
    HOSTAWAY_DIRECT_CHANNEL_ID=2000,
)
def test_repeated_pre_post_blocker_updates_operation_error() -> None:
    intent = make_intent(listing_map_id=None)
    reservation = prepare_local_reservation(intent)
    successful_payment(intent)
    service = HostawayBookingService(
        client=CreateClientStub(),
        availability_service=AvailabilityStub(complete_availability(intent)),
    )
    first = service.create_hostaway_reservation(reservation)
    assert first.operation is not None
    assert first.operation.error_code == "listing_map_id_not_verified"

    reservation.property.hostaway_listing_map_id = 9001
    reservation.property.save(update_fields=["hostaway_listing_map_id"])
    missing_total = complete_availability(intent)
    assert missing_total.quote is not None
    component = replace(missing_total.quote.components[0], total=None)
    service.availability_service = AvailabilityStub(
        replace(missing_total, quote=replace(missing_total.quote, components=(component,)))
    )
    second = service.create_hostaway_reservation(reservation)

    assert second.code == "price_component_total_missing"
    assert second.operation is not None
    assert second.operation.pk == first.operation.pk
    assert second.operation.error_code == "price_component_total_missing"
    assert second.operation.attempt_count == 0


@pytest.mark.parametrize(
    ("total", "currency", "code"),
    [
        (Decimal("501.00"), "SAR", "revalidated_price_changed"),
        (Decimal("500.25"), "USD", "revalidated_price_changed"),
    ],
)
@override_settings(
    HOSTAWAY_LIVE_BOOKING_ENABLED=True,
    HOSTAWAY_DIRECT_CHANNEL_ID=2000,
)
def test_price_or_currency_change_prevents_post(
    total: Decimal,
    currency: str,
    code: str,
) -> None:
    intent = make_intent()
    reservation = prepare_local_reservation(intent)
    successful_payment(intent)
    result = complete_availability(intent, total=total)
    assert result.quote is not None
    result = replace(result, quote=replace(result.quote, currency=currency))
    client = CreateClientStub()
    outcome = HostawayBookingService(
        client=client,
        availability_service=AvailabilityStub(result),
    ).create_hostaway_reservation(reservation)
    assert outcome.code == code
    assert client.calls == 0


@override_settings(
    HOSTAWAY_LIVE_BOOKING_ENABLED=True,
    HOSTAWAY_DIRECT_CHANNEL_ID=2000,
)
def test_lost_availability_prevents_post() -> None:
    intent = make_intent()
    reservation = prepare_local_reservation(intent)
    successful_payment(intent)
    unavailable = make_availability(intent.property, available=False)
    client = CreateClientStub()
    outcome = HostawayBookingService(
        client=client,
        availability_service=AvailabilityStub(unavailable),
    ).create_hostaway_reservation(reservation)
    assert outcome.code == "availability_lost"
    assert client.calls == 0


@override_settings(
    HOSTAWAY_LIVE_BOOKING_ENABLED=True,
    HOSTAWAY_DIRECT_CHANNEL_ID=2000,
)
def test_success_confirms_once_and_network_is_outside_transaction() -> None:
    intent = make_intent()
    reservation = prepare_local_reservation(intent)
    successful_payment(intent)
    client = CreateClientStub(create_result(reservation))
    service = HostawayBookingService(
        client=client,
        availability_service=AvailabilityStub(complete_availability(intent)),
    )
    outer_savepoint_depth = len(connection.savepoint_ids)
    first = service.create_hostaway_reservation(reservation)
    second = service.create_hostaway_reservation(first.reservation)
    first.reservation.refresh_from_db()
    intent.refresh_from_db()
    assert first.code == "confirmed"
    assert second.code == "already_confirmed"
    assert client.calls == 1
    assert client.savepoint_depth == outer_savepoint_depth
    assert first.reservation.hostaway_reservation_id == 77001
    assert first.reservation.normalized_status == Reservation.Status.CONFIRMED
    assert intent.status == BookingIntent.Status.COMPLETED
    assert HostawayReservationOperation.objects.count() == 1


@override_settings(
    HOSTAWAY_LIVE_BOOKING_ENABLED=True,
    HOSTAWAY_DIRECT_CHANNEL_ID=2000,
)
def test_hostaway_unknown_does_not_downgrade_verified_local_payment() -> None:
    intent = make_intent()
    reservation = prepare_local_reservation(intent)
    successful_payment(intent)
    reservation.payment_status = "paid"
    reservation.save(update_fields=["payment_status", "updated_at"])
    result = create_result(reservation)
    result = replace(
        result,
        snapshot=replace(result.snapshot, payment_status="Unknown"),
    )

    outcome = HostawayBookingService(
        client=CreateClientStub(result),
        availability_service=AvailabilityStub(complete_availability(intent)),
    ).create_hostaway_reservation(reservation)

    assert outcome.code == "confirmed"
    assert outcome.reservation.payment_status == "paid"


@override_settings(
    HOSTAWAY_LIVE_BOOKING_ENABLED=True,
    HOSTAWAY_DIRECT_CHANNEL_ID=2000,
)
def test_timeout_becomes_unknown_and_is_never_reposted(
    caplog: pytest.LogCaptureFixture,
) -> None:
    intent = make_intent()
    reservation = prepare_local_reservation(intent)
    successful_payment(intent)
    client = CreateClientStub(error=HostawayTimeoutError("synthetic timeout"))
    service = HostawayBookingService(
        client=client,
        availability_service=AvailabilityStub(complete_availability(intent)),
    )
    first = service.create_hostaway_reservation(reservation)
    second = service.create_hostaway_reservation(first.reservation)
    assert first.code == "hostaway_create_uncertain"
    assert second.operation.status == HostawayReservationOperation.Status.UNKNOWN
    assert client.calls == 1
    assert first.reservation.normalized_status == Reservation.Status.CREATE_UNKNOWN
    intent.refresh_from_db()
    assert intent.status == BookingIntent.Status.AWAITING_PAYMENT
    assert intent.guest_email not in caplog.text
    assert intent.guest_phone not in caplog.text
    assert intent.guest_first_name not in caplog.text
    assert intent.guest_last_name not in caplog.text


@override_settings(
    HOSTAWAY_LIVE_BOOKING_ENABLED=True,
    HOSTAWAY_DIRECT_CHANNEL_ID=2000,
)
def test_confirmed_rejection_fails_without_retrying() -> None:
    intent = make_intent()
    reservation = prepare_local_reservation(intent)
    successful_payment(intent)
    client = CreateClientStub(error=HostawayAuthenticationError("synthetic rejection"))
    outcome = HostawayBookingService(
        client=client,
        availability_service=AvailabilityStub(complete_availability(intent)),
    ).create_hostaway_reservation(reservation)
    assert outcome.code == "hostaway_create_rejected"
    assert outcome.reservation.normalized_status == Reservation.Status.CREATE_FAILED
    assert outcome.operation is not None
    assert outcome.operation.status == HostawayReservationOperation.Status.FAILED
    assert client.calls == 1
    intent.refresh_from_db()
    assert intent.status == BookingIntent.Status.AWAITING_PAYMENT


@override_settings(
    HOSTAWAY_LIVE_BOOKING_ENABLED=True,
    HOSTAWAY_DIRECT_CHANNEL_ID=2000,
)
def test_in_progress_operation_prevents_second_post() -> None:
    intent = make_intent()
    reservation = prepare_local_reservation(intent)
    successful_payment(intent)
    HostawayReservationOperation.objects.create(
        reservation=reservation,
        operation_type=HostawayReservationOperation.OperationType.CREATE_RESERVATION,
        idempotency_key="phase5-operation-in-progress",
        request_fingerprint="a" * 64,
        status=HostawayReservationOperation.Status.IN_PROGRESS,
        attempt_count=1,
    )
    client = CreateClientStub()
    outcome = HostawayBookingService(
        client=client,
        availability_service=AvailabilityStub(complete_availability(intent)),
    ).create_hostaway_reservation(reservation)
    assert outcome.code == HostawayReservationOperation.Status.IN_PROGRESS
    assert client.calls == 0


def test_client_refuses_create_when_flag_disabled() -> None:
    client = HostawayClient(
        client=httpx.Client(transport=httpx.MockTransport(lambda request: None)),
        token_provider=Mock(),
    )
    with pytest.raises(HostawayConfigurationError, match="hostaway_live_booking_disabled"):
        client.create_reservation_with_price_details(Mock())
    client.close()


class TokenStub:
    masked_account_id = "****0001"

    def get_token(self, *, force_refresh: bool = False) -> str:
        return "synthetic-token"

    def invalidate(self) -> None:
        pass

    def close(self) -> None:
        pass


def client_request() -> HostawayReservationCreateRequest:
    return HostawayReservationCreateRequest(
        listing_map_id=9001,
        channel_id=2000,
        guest_first_name="Test",
        guest_last_name="Guest",
        guest_email="test@example.invalid",
        guest_phone="+966500000000",
        guest_country_code="SA",
        guests=2,
        check_in=timezone.localdate() + timedelta(days=10),
        check_out=timezone.localdate() + timedelta(days=12),
        currency="SAR",
        total_price=Decimal("500.25"),
        finance_fields=(
            ReservationFinanceField(
                listing_fee_setting_id=1,
                type="accommodation",
                name="baseRate",
                title="Base rate",
                alias="base",
                quantity=None,
                value=Decimal("500.25"),
                total=Decimal("500.25"),
                is_included_in_total_price=True,
                is_overridden_by_user=False,
                is_mandatory=True,
                is_deleted=False,
            ),
        ),
        provider="LuxurySmartApartments",
    )


def reservation_response(request: HostawayReservationCreateRequest) -> dict[str, object]:
    return {
        "status": "success",
        "result": {
            "id": 77001,
            "hostawayReservationId": 77001,
            "listingMapId": request.listing_map_id,
            "channelId": request.channel_id,
            "status": "new",
            "arrivalDate": request.check_in.isoformat(),
            "departureDate": request.check_out.isoformat(),
            "numberOfGuests": request.guests,
            "currency": request.currency,
            "totalPrice": str(request.total_price),
            "guestEmail": "must-not-enter-dto@example.invalid",
            "phone": "+966511111111",
            "doorCode": "secret",
        },
    }


@override_settings(HOSTAWAY_LIVE_BOOKING_ENABLED=True)
def test_client_create_uses_one_safe_post_without_forbidden_query_or_fields() -> None:
    dto = client_request()
    observed: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        observed.append(request)
        body = request.content.decode()
        assert request.method == "POST"
        assert request.url.path == "/v1/reservations"
        assert dict(request.url.params) == {"provider": "LuxurySmartApartments"}
        assert request.headers["Content-Type"] == "application/json"
        for forbidden in ("forceOverbooking", "validatePaymentMethod", "ccNumber", "cvc"):
            assert forbidden not in str(request.url)
            assert forbidden not in body
        return httpx.Response(200, json=reservation_response(dto))

    http_client = httpx.Client(
        base_url="https://api.hostaway.com/v1",
        transport=httpx.MockTransport(handler),
    )
    client = HostawayClient(client=http_client, token_provider=TokenStub())
    result = client.create_reservation_with_price_details(dto)
    assert result.snapshot.reservation_id == 77001
    assert len(observed) == 1
    assert not hasattr(result.snapshot, "guest_email")
    http_client.close()


def test_client_get_reservation_is_sanitized_get_only() -> None:
    dto = client_request()

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.path == "/v1/reservations/77001"
        return httpx.Response(200, json=reservation_response(dto))

    http_client = httpx.Client(
        base_url="https://api.hostaway.com/v1",
        transport=httpx.MockTransport(handler),
    )
    client = HostawayClient(client=http_client, token_provider=TokenStub())
    snapshot = client.get_reservation(77001)
    assert snapshot.reservation_id == 77001
    assert not hasattr(snapshot, "door_code")
    http_client.close()


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        # Active stays.
        ("new", "confirmed"),
        ("confirmed", "confirmed"),
        ("ownerStay", "confirmed"),
        ("modified", "modified"),
        # Booked, not settled.
        ("awaitingPayment", "awaiting_payment"),
        ("pending", "pending"),
        ("unconfirmed", "pending"),
        # Leads.
        ("inquiry", "inquiry"),
        ("inquiryPreapproved", "inquiry"),
        # Closed without a stay.
        ("cancelled", "cancelled"),
        ("canceled", "cancelled"),
        ("declined", "declined"),
        ("inquiryDenied", "declined"),
        ("inquiryNotPossible", "declined"),
        ("expired", "expired"),
        ("inquiryTimeout", "expired"),
        # Casing and padding must not change the outcome.
        ("  INQUIRYNOTPOSSIBLE  ", "declined"),
        # Only a status Hostaway has not published yet stays unknown.
        ("futureStatus", "unknown"),
    ],
)
def test_status_mapping(raw: str, expected: str) -> None:
    assert normalize_hostaway_reservation_status(raw) == expected


def test_every_mapped_status_is_a_declared_reservation_status() -> None:
    """A typo in the map would otherwise fail only later, at full_clean time."""
    declared = {choice.value for choice in Reservation.Status}

    assert set(HOSTAWAY_RESERVATION_STATUS_MAP.values()) <= declared


def test_status_filter_from_the_hostaway_dashboard_is_fully_covered() -> None:
    """Every status the Hostaway reservation filter offers, in its API spelling."""
    dashboard_statuses = {
        "confirmed",
        "new",
        "ownerStay",
        "modified",
        "cancelled",
        "pending",
        "unconfirmed",
        "awaitingPayment",
        "declined",
        "expired",
        "inquiry",
        "inquiryPreapproved",
        "inquiryDenied",
        "inquiryTimeout",
        "inquiryNotPossible",
    }

    unmapped = {
        status
        for status in dashboard_statuses
        if normalize_hostaway_reservation_status(status) == "unknown"
    }

    assert not unmapped


def test_verify_prerequisites_is_read_only(monkeypatch: pytest.MonkeyPatch) -> None:
    property_obj = make_property()
    before = (
        Reservation.objects.count(),
        HostawayReservationOperation.objects.count(),
        PaymentAttempt.objects.count(),
    )

    class Document:
        record = {"id": property_obj.hostaway_listing_id, "name": "Synthetic"}

    client = Mock()
    client.__enter__ = Mock(return_value=client)
    client.__exit__ = Mock(return_value=None)
    client.get_listing_document.return_value = Document()
    monkeypatch.setattr(
        "apps.reservations.management.commands.verify_hostaway_booking_prerequisites.HostawayClient",
        Mock(return_value=client),
    )
    output = StringIO()
    call_command(
        "verify_hostaway_booking_prerequisites",
        listing_id=property_obj.hostaway_listing_id,
        strict=True,
        stdout=output,
    )
    assert "Reservation POST calls: 0" in output.getvalue()
    assert "hostaway_live_booking_disabled" in output.getvalue()
    assert (
        Reservation.objects.count(),
        HostawayReservationOperation.objects.count(),
        PaymentAttempt.objects.count(),
    ) == before


def test_no_raw_payload_fields_exist_on_operation() -> None:
    fields = {field.name for field in HostawayReservationOperation._meta.fields}
    assert "request_payload" not in fields
    assert "response_payload" not in fields
    assert "request_fingerprint" in fields
