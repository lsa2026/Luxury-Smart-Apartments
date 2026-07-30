from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
from io import StringIO

import pytest
from django.core.cache import cache
from django.core.management import call_command
from django.test import Client, override_settings
from django.utils import timezone

from apps.integrations.hostaway.availability_validators import (
    CalendarDay,
    CalendarDocument,
    PriceComponent,
    PriceQuote,
)
from apps.integrations.hostaway.exceptions import HostawayTimeoutError
from apps.integrations.hostaway.reservation_validators import (
    HostawayReservationSnapshot,
)
from apps.payments.models import PaymentAttempt
from apps.reservations.models import (
    BookingModificationRequest,
    HostawayModificationOperation,
    Reservation,
)
from apps.reservations.security import SESSION_MARKER_KEY, hash_session_marker
from apps.reservations.services.availability import (
    AVAILABLE,
    AvailabilityResult,
    CalendarFetch,
)
from apps.reservations.services.hostaway_booking import prepare_local_reservation
from apps.reservations.services.hostaway_modifications import (
    HostawayModificationService,
)
from apps.reservations.services.modifications import ModificationService
from tests.test_hostaway_booking_phase5 import make_intent

pytestmark = pytest.mark.django_db


def confirmed_reservation(*, source: str = Reservation.SourceType.DIRECT_WEBSITE) -> Reservation:
    intent = make_intent()
    reservation = prepare_local_reservation(intent)
    reservation.hostaway_reservation_id = 77001
    reservation.normalized_status = Reservation.Status.CONFIRMED
    reservation.source_type = source
    reservation.confirmed_at = timezone.now()
    reservation.save()
    return reservation


def price_quote(
    reservation: Reservation,
    *,
    check_in=None,
    check_out=None,
    guests: int | None = None,
    total: Decimal = Decimal("650.25"),
    currency: str = "SAR",
) -> PriceQuote:
    check_in = check_in or reservation.check_in
    check_out = check_out or reservation.check_out + timedelta(days=2)
    return PriceQuote(
        listing_id=reservation.property.hostaway_listing_id,
        check_in=check_in,
        check_out=check_out,
        nights=(check_out - check_in).days,
        guests=guests or reservation.guests,
        currency=currency,
        total_price=total,
        components=(
            PriceComponent(
                listing_fee_setting_id=999,
                type="price",
                name="baseRate",
                title="Base rate",
                alias="base",
                quantity=None,
                value=total,
                total=total,
                is_included_in_total=True,
                is_overridden_by_user=False,
                is_mandatory=True,
                is_deleted=False,
            ),
        ),
        calculated_at=timezone.now(),
        envelope_fields=frozenset(),
        result_field_types=(),
        component_field_types=(),
    )


def calendar_day(day, **changes) -> CalendarDay:
    return replace(
        CalendarDay(
            date=day,
            is_available=True,
            price=Decimal("100"),
            minimum_stay=1,
            maximum_stay=30,
            closed_on_arrival=False,
            closed_on_departure=False,
            status="available",
            available_units_to_sell=1,
        ),
        **changes,
    )


class ModificationAvailabilityStub:
    def __init__(
        self,
        reservation: Reservation,
        *,
        quote: PriceQuote | None = None,
        day_changes: dict[int, dict[str, object]] | None = None,
        available: bool = True,
        reason_code: str = AVAILABLE,
    ) -> None:
        self.reservation = reservation
        self.quote = quote or price_quote(reservation)
        self.day_changes = day_changes or {}
        self.available = available
        self.reason_code = reason_code
        self.client = self
        self.price_calls = 0
        self.calendar_calls = 0

    def fetch_calendar(
        self,
        *,
        property_obj,
        start_date,
        end_date,
        bypass_cache,
    ) -> CalendarFetch:
        assert property_obj == self.reservation.property
        assert bypass_cache is True
        self.calendar_calls += 1
        days = tuple(
            calendar_day(
                start_date + timedelta(days=offset),
                **self.day_changes.get(offset, {}),
            )
            for offset in range((end_date - start_date).days + 1)
        )
        return CalendarFetch(CalendarDocument(days, frozenset(), ()), 1, False)

    def calculate_price(self, *args, **kwargs) -> PriceQuote:
        self.price_calls += 1
        return self.quote

    def check(self, request, *, bypass_cache):
        assert bypass_cache is True
        quote = self.quote
        if self.available:
            quote = replace(
                quote,
                check_in=request.check_in,
                check_out=request.check_out,
                nights=(request.check_out - request.check_in).days,
                guests=request.guests,
            )
        return AvailabilityResult(
            is_available=self.available,
            reason_code=self.reason_code,
            user_message_ar="",
            user_message_en="",
            nights=(request.check_out - request.check_in).days,
            quote=quote if self.available else None,
        )

    def close(self) -> None:
        pass


def create_extension(
    reservation: Reservation,
    *,
    stub: ModificationAvailabilityStub | None = None,
    added_nights: int = 2,
):
    stub = stub or ModificationAvailabilityStub(reservation)
    service = ModificationService(availability_service=stub)
    return service.create_extension_quote(
        reservation,
        new_check_out=reservation.check_out + timedelta(days=added_nights),
        session_hash=reservation.booking_intent.session_key_hash,
        reason="<script>alert(1)</script> Family stay",
    )


def test_create_extension_quote_is_local_decimal_and_sanitized() -> None:
    reservation = confirmed_reservation()
    original = (reservation.check_out, reservation.total_price)
    outcome = create_extension(reservation)
    modification = outcome.request
    assert outcome.code == "created"
    assert modification is not None
    assert modification.request_type == BookingModificationRequest.RequestType.EXTEND_STAY
    assert modification.price_difference == Decimal("150.0000")
    assert modification.status == BookingModificationRequest.Status.AWAITING_PAYMENT
    assert "<script>" not in modification.reason
    assert modification.quote_snapshot["price_version"] == 2
    assert "raw" not in modification.quote_snapshot
    reservation.refresh_from_db()
    assert (reservation.check_out, reservation.total_price) == original
    assert PaymentAttempt.objects.count() == 0


@pytest.mark.parametrize(
    ("added_nights", "code"),
    [(0, "extension_must_add_nights"), (31, "extension_limit_exceeded")],
)
def test_extension_date_and_limit_validation(added_nights: int, code: str) -> None:
    reservation = confirmed_reservation()
    outcome = create_extension(reservation, added_nights=added_nights)
    assert outcome.code == code
    assert BookingModificationRequest.objects.count() == 0


@pytest.mark.parametrize(
    ("changes", "expected"),
    [
        ({0: {"is_available": False}}, "inventory_conflict"),
        ({0: {"minimum_stay": 3}}, "minimum_stay_not_met"),
        ({0: {"maximum_stay": 1}}, "maximum_stay_exceeded"),
        ({0: {"available_units_to_sell": 0}}, "inventory_conflict"),
    ],
)
def test_extension_calendar_restrictions(
    changes: dict[int, dict[str, object]],
    expected: str,
) -> None:
    reservation = confirmed_reservation()
    stub = ModificationAvailabilityStub(reservation, day_changes=changes)
    outcome = create_extension(reservation, stub=stub)
    assert outcome.code == expected
    assert stub.price_calls == 0


@pytest.mark.parametrize(
    ("total", "expected_status", "difference"),
    [
        (
            Decimal("500.25"),
            BookingModificationRequest.Status.PENDING_ADMIN_APPROVAL,
            Decimal("0"),
        ),
        (
            Decimal("450.25"),
            BookingModificationRequest.Status.PENDING_ADMIN_APPROVAL,
            Decimal("-50"),
        ),
    ],
)
def test_zero_and_negative_price_difference_require_admin(
    total: Decimal,
    expected_status: str,
    difference: Decimal,
) -> None:
    reservation = confirmed_reservation()
    stub = ModificationAvailabilityStub(
        reservation,
        quote=price_quote(reservation, total=total),
    )
    modification = create_extension(reservation, stub=stub).request
    assert modification is not None
    assert modification.status == expected_status
    assert modification.price_difference == difference
    assert PaymentAttempt.objects.count() == 0


def test_currency_change_blocks_request() -> None:
    reservation = confirmed_reservation()
    stub = ModificationAvailabilityStub(
        reservation,
        quote=price_quote(reservation, currency="USD"),
    )
    outcome = create_extension(reservation, stub=stub)
    assert outcome.code == "currency_changed"
    assert BookingModificationRequest.objects.count() == 0


@pytest.mark.parametrize(
    ("mutator", "code"),
    [
        (
            lambda item: setattr(item, "normalized_status", Reservation.Status.MODIFIED),
            "reservation_not_confirmed",
        ),
        (
            lambda item: setattr(item, "source_type", Reservation.SourceType.EXTERNAL_CHANNEL),
            "external_channel_requires_admin",
        ),
    ],
)
def test_nonconfirmed_and_external_reservations_are_blocked(mutator, code: str) -> None:
    reservation = confirmed_reservation()
    mutator(reservation)
    reservation.save()
    outcome = create_extension(reservation)
    assert outcome.code == code


def test_session_ownership_and_idempotency() -> None:
    reservation = confirmed_reservation()
    wrong = ModificationService(
        availability_service=ModificationAvailabilityStub(reservation)
    ).create_extension_quote(
        reservation,
        new_check_out=reservation.check_out + timedelta(days=2),
        session_hash="wrong" * 12 + "xxxx",
    )
    assert wrong.code == "not_found"
    first = create_extension(reservation)
    second = create_extension(reservation)
    assert first.request == second.request
    assert second.code == "idempotent"
    assert BookingModificationRequest.objects.count() == 1


def test_change_guests_capacity_and_change_dates() -> None:
    reservation = confirmed_reservation()
    stub = ModificationAvailabilityStub(reservation)
    service = ModificationService(availability_service=stub)
    too_many = service.create_change_quote(
        reservation,
        new_check_in=reservation.check_in,
        new_check_out=reservation.check_out,
        new_guests=99,
        session_hash=reservation.booking_intent.session_key_hash,
    )
    assert too_many.code == "capacity_exceeded"
    changed = service.create_change_quote(
        reservation,
        new_check_in=reservation.check_in + timedelta(days=1),
        new_check_out=reservation.check_out + timedelta(days=1),
        new_guests=3,
        session_hash=reservation.booking_intent.session_key_hash,
    )
    assert changed.request is not None
    assert changed.request.request_type == BookingModificationRequest.RequestType.CHANGE_DATES


def test_cancellation_request_is_local_and_external_is_blocked() -> None:
    reservation = confirmed_reservation()
    service = ModificationService(availability_service=ModificationAvailabilityStub(reservation))
    result = service.create_cancellation_request(
        reservation,
        session_hash=reservation.booking_intent.session_key_hash,
        reason="Please cancel",
    )
    assert result.request is not None
    assert result.request.status == BookingModificationRequest.Status.PENDING_ADMIN_APPROVAL
    reservation.refresh_from_db()
    assert reservation.normalized_status == Reservation.Status.CONFIRMED
    assert PaymentAttempt.objects.count() == 0
    reservation.source_type = Reservation.SourceType.EXTERNAL_CHANNEL
    reservation.save()
    blocked = service.create_cancellation_request(
        reservation,
        session_hash=reservation.booking_intent.session_key_hash,
    )
    assert blocked.code == "external_channel_requires_admin"


class WriteClientStub:
    def __init__(self, snapshot=None, error=None) -> None:
        self.snapshot = snapshot
        self.error = error
        self.calls = 0

    def update_reservation(self, *args, **kwargs):
        self.calls += 1
        if self.error:
            raise self.error
        return self.snapshot

    def cancel_reservation(self, *args, **kwargs):
        return self.update_reservation(*args, **kwargs)

    def close(self) -> None:
        pass


def ready_extension(reservation: Reservation) -> BookingModificationRequest:
    modification = create_extension(reservation).request
    assert modification is not None
    modification.status = BookingModificationRequest.Status.READY_FOR_HOSTAWAY
    modification.save(update_fields=["status"])
    return modification


def updated_snapshot(modification: BookingModificationRequest) -> HostawayReservationSnapshot:
    return HostawayReservationSnapshot(
        reservation_id=modification.reservation.hostaway_reservation_id,
        listing_map_id=modification.reservation.hostaway_listing_map_id,
        channel_id=2000,
        status="modified",
        check_in=modification.new_check_in,
        check_out=modification.new_check_out,
        guests=modification.new_guests,
        currency=modification.currency,
        total_price=modification.new_total,
        payment_status="paid",
        source="LuxurySmartApartments",
        updated_at=timezone.now(),
    )


def test_live_flags_block_hostaway_write() -> None:
    modification = ready_extension(confirmed_reservation())
    client = WriteClientStub()
    outcome = HostawayModificationService(client=client).execute(modification)
    assert outcome.code == "hostaway_live_modification_disabled"
    assert client.calls == 0


@override_settings(
    HOSTAWAY_LIVE_MODIFICATION_ENABLED=True,
    HOSTAWAY_LIVE_EXTENSION_ENABLED=True,
)
def test_timeout_becomes_unknown_and_is_not_retried() -> None:
    modification = ready_extension(confirmed_reservation())
    client = WriteClientStub(error=HostawayTimeoutError("synthetic timeout"))
    service = HostawayModificationService(client=client)
    first = service.execute(modification)
    second = service.execute(first.request)
    assert first.code == "hostaway_modification_uncertain"
    assert first.request.status == BookingModificationRequest.Status.UNKNOWN
    assert second.code == HostawayModificationOperation.Status.UNKNOWN
    assert client.calls == 1


@override_settings(
    HOSTAWAY_LIVE_MODIFICATION_ENABLED=True,
    HOSTAWAY_LIVE_EXTENSION_ENABLED=True,
)
def test_success_reconciles_once() -> None:
    reservation = confirmed_reservation()
    modification = ready_extension(reservation)
    client = WriteClientStub(snapshot=updated_snapshot(modification))
    service = HostawayModificationService(client=client)
    first = service.execute(modification)
    second = service.execute(first.request)
    reservation.refresh_from_db()
    assert first.code == "completed"
    assert second.code == "modification_not_ready"
    assert client.calls == 1
    assert reservation.check_out == modification.new_check_out
    assert HostawayModificationOperation.objects.count() == 1


def test_expire_command_and_dry_run() -> None:
    modification = create_extension(confirmed_reservation()).request
    assert modification is not None
    modification.expires_at = timezone.now() - timedelta(seconds=1)
    modification.save(update_fields=["expires_at"])
    output = StringIO()
    call_command("expire_booking_modification_requests", dry_run=True, stdout=output)
    modification.refresh_from_db()
    assert modification.status != BookingModificationRequest.Status.EXPIRED
    call_command("expire_booking_modification_requests", stdout=output)
    modification.refresh_from_db()
    assert modification.status == BookingModificationRequest.Status.EXPIRED
    assert "Hostaway calls: 0" in output.getvalue()


def owned_web_reservation() -> tuple[Client, Reservation]:
    reservation = confirmed_reservation()
    marker = "synthetic-session-marker"
    reservation.booking_intent.session_key_hash = hash_session_marker(marker)
    reservation.booking_intent.save(update_fields=["session_key_hash"])
    client = Client()
    session = client.session
    session[SESSION_MARKER_KEY] = marker
    session.save()
    return client, reservation


def test_manage_page_is_rtl_session_owned_and_hides_hostaway_ids() -> None:
    client, reservation = owned_web_reservation()
    response = client.get(f"/reservations/manage/{reservation.public_reference}/")
    assert response.status_code == 200
    content = response.content.decode()
    assert 'dir="rtl"' in content
    assert "طلبك قيد المراجعة ولم يتم تعديل الحجز بعد." in content
    assert str(reservation.hostaway_reservation_id) not in content
    assert Client().get(f"/reservations/manage/{reservation.public_reference}/").status_code == 404


def test_modification_posts_require_csrf() -> None:
    client, reservation = owned_web_reservation()
    secure_client = Client(enforce_csrf_checks=True)
    secure_client.cookies = client.cookies
    response = secure_client.post(
        f"/reservations/manage/{reservation.public_reference}/cancel/",
        {"confirm": "on"},
    )
    assert response.status_code == 403


@override_settings(
    BOOKING_MODIFICATION_RATE_LIMIT_REQUESTS=1,
    BOOKING_MODIFICATION_RATE_LIMIT_WINDOW=600,
)
def test_modification_rate_limit_and_xss_cleaning() -> None:
    cache.clear()
    client, reservation = owned_web_reservation()
    url = f"/reservations/manage/{reservation.public_reference}/cancel/"
    first = client.post(
        url,
        {
            "confirm": "on",
            "reason": "<img src=x onerror=alert(1)> Please cancel",
        },
    )
    assert first.status_code == 302
    modification = BookingModificationRequest.objects.get()
    assert "<img" not in modification.reason
    second = client.post(url, {"confirm": "on"})
    assert second.status_code == 429


def test_other_session_cannot_read_modification() -> None:
    client, reservation = owned_web_reservation()
    modification = (
        ModificationService(availability_service=ModificationAvailabilityStub(reservation))
        .create_cancellation_request(
            reservation,
            session_hash=reservation.booking_intent.session_key_hash,
        )
        .request
    )
    assert modification is not None
    url = f"/reservations/modifications/{modification.public_reference}/"
    assert client.get(url).status_code == 200
    assert Client().get(url).status_code == 404


def test_modification_models_have_no_raw_payload_or_payment_fields() -> None:
    request_fields = {field.name for field in BookingModificationRequest._meta.fields}
    operation_fields = {field.name for field in HostawayModificationOperation._meta.fields}
    assert not {"raw_payload", "raw_response", "card_number", "refund_amount"} & request_fields
    assert not {"raw_payload", "raw_response", "authorization"} & operation_fields


@override_settings(BOOKING_CANCELLATION_REQUEST_ENABLED=False)
def test_local_cancellation_request_flag_can_disable_intake() -> None:
    reservation = confirmed_reservation()
    outcome = ModificationService(
        availability_service=ModificationAvailabilityStub(reservation)
    ).create_cancellation_request(
        reservation,
        session_hash=reservation.booking_intent.session_key_hash,
    )
    assert outcome.code == "cancellation_requests_disabled"
    assert BookingModificationRequest.objects.count() == 0
