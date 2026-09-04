import base64
import json
from datetime import date, timedelta
from decimal import Decimal
from io import StringIO

import pytest
from django.core.cache import cache
from django.core.management import call_command
from django.test import Client, override_settings
from django.utils import timezone

from apps.integrations.hostaway.exceptions import (
    HostawayNotFoundError,
    HostawayResponseError,
)
from apps.integrations.hostaway.reservation_validators import (
    HostawayReservationSnapshot,
)
from apps.integrations.hostaway.webhook_processor import process_webhook_event
from apps.integrations.models import HostawayWebhookEvent
from apps.properties.models import Property
from apps.reservations.models import Reservation
from apps.reservations.services.hostaway_booking import prepare_local_reservation
from tests.test_booking_models_services import make_property
from tests.test_hostaway_booking_phase5 import make_intent

pytestmark = pytest.mark.django_db

WEBHOOK_URL = "/integrations/hostaway/webhooks/unified/"
WEBHOOK_SETTINGS = {
    "HOSTAWAY_WEBHOOK_RECEIVER_ENABLED": True,
    "HOSTAWAY_WEBHOOK_BASIC_AUTH_USERNAME": "synthetic-user",
    "HOSTAWAY_WEBHOOK_BASIC_AUTH_PASSWORD": "synthetic-password",
    "HOSTAWAY_WEBHOOK_ALLOWED_EVENTS": (
        "reservation.created",
        "reservation.updated",
    ),
}


def auth_header(username: str = "synthetic-user", password: str = "synthetic-password") -> str:
    encoded = base64.b64encode(f"{username}:{password}".encode()).decode()
    return f"Basic {encoded}"


def webhook_payload(
    *,
    event_type: str = "reservation created",
    reservation_id: int = 88001,
    event_id: str | None = "evt-synthetic-1",
    updated_on: str = "2026-08-01 10:00:00",
) -> dict[str, object]:
    payload: dict[str, object] = {
        "eventType": event_type,
        "objectId": reservation_id,
        "data": {
            "hostawayReservationId": reservation_id,
            "listingMapId": 9001,
            "status": "new",
            "arrivalDate": "2026-11-23",
            "departureDate": "2026-11-25",
            "totalPrice": "500.25",
            "currency": "SAR",
            "paymentStatus": "paid",
            "updatedOn": updated_on,
            "guestEmail": "private@example.invalid",
            "phone": "+966500000000",
            "guestName": "Synthetic Guest",
            "doorCode": "1234",
            "hostNote": "private",
            "guestNote": "private",
            "customFieldValues": [{"private": "value"}],
        },
    }
    if event_id:
        payload["eventId"] = event_id
    return payload


def post_webhook(
    payload: object,
    *,
    authorization: str | None = None,
    content_type: str = "application/json",
) -> object:
    headers = {}
    if authorization is not None:
        headers["HTTP_AUTHORIZATION"] = authorization
    return Client().post(
        WEBHOOK_URL,
        data=json.dumps(payload),
        content_type=content_type,
        **headers,
    )


@override_settings(**WEBHOOK_SETTINGS)
def test_basic_auth_valid_event_is_sanitized_and_accepted() -> None:
    response = post_webhook(webhook_payload(), authorization=auth_header())
    assert response.status_code == 202
    event = HostawayWebhookEvent.objects.get()
    serialized = json.dumps(event.sanitized_payload)
    assert event.event_type == "reservation.created"
    assert event.hostaway_reservation_id == 88001
    for forbidden in (
        "guestEmail",
        "phone",
        "guestName",
        "doorCode",
        "hostNote",
        "guestNote",
        "customFieldValues",
        "private@example.invalid",
    ):
        assert forbidden not in serialized


@override_settings(**WEBHOOK_SETTINGS)
@pytest.mark.parametrize(
    "authorization",
    [None, "Basic invalid", auth_header(password="wrong")],
)
def test_invalid_basic_auth_is_401(authorization: str | None) -> None:
    response = post_webhook(webhook_payload(), authorization=authorization)
    assert response.status_code == 401
    assert HostawayWebhookEvent.objects.count() == 0


def test_receiver_disabled_is_404() -> None:
    response = post_webhook(webhook_payload(), authorization=auth_header())
    assert response.status_code == 404


@override_settings(**WEBHOOK_SETTINGS)
def test_invalid_content_type_and_json_are_rejected() -> None:
    wrong_type = post_webhook(
        webhook_payload(),
        authorization=auth_header(),
        content_type="text/plain",
    )
    invalid_json = Client().post(
        WEBHOOK_URL,
        data="{",
        content_type="application/json",
        HTTP_AUTHORIZATION=auth_header(),
    )
    assert wrong_type.status_code == 400
    assert invalid_json.status_code == 400
    assert HostawayWebhookEvent.objects.count() == 0


@override_settings(**WEBHOOK_SETTINGS, HOSTAWAY_WEBHOOK_MAX_BODY_BYTES=64)
def test_oversized_body_is_rejected() -> None:
    response = post_webhook(webhook_payload(), authorization=auth_header())
    assert response.status_code == 400
    assert HostawayWebhookEvent.objects.count() == 0


@override_settings(**WEBHOOK_SETTINGS)
def test_duplicate_event_is_idempotent() -> None:
    first = post_webhook(webhook_payload(), authorization=auth_header())
    second = post_webhook(webhook_payload(), authorization=auth_header())
    assert first.status_code == 202
    assert second.status_code == 200
    assert second.json()["status"] == "duplicate"
    assert HostawayWebhookEvent.objects.count() == 1


@override_settings(**WEBHOOK_SETTINGS)
def test_deduplication_without_external_event_id() -> None:
    payload = webhook_payload(event_id=None)
    post_webhook(payload, authorization=auth_header())
    post_webhook(payload, authorization=auth_header())
    assert HostawayWebhookEvent.objects.count() == 1


@override_settings(**WEBHOOK_SETTINGS)
def test_unknown_event_is_ignored_with_200() -> None:
    response = post_webhook(
        webhook_payload(event_type="future object changed"),
        authorization=auth_header(),
    )
    event = HostawayWebhookEvent.objects.get()
    assert response.status_code == 200
    assert event.status == HostawayWebhookEvent.Status.IGNORED
    assert event.error_code == "unsupported_event"


@override_settings(
    **WEBHOOK_SETTINGS,
    HOSTAWAY_WEBHOOK_RATE_LIMIT_REQUESTS=1,
    HOSTAWAY_WEBHOOK_RATE_LIMIT_WINDOW=60,
)
def test_webhook_rate_limit_is_defensive() -> None:
    cache.clear()
    first = post_webhook(webhook_payload(), authorization=auth_header())
    second = post_webhook(
        webhook_payload(event_id="evt-2", reservation_id=88002),
        authorization=auth_header(),
    )
    assert first.status_code == 202
    assert second.status_code == 503


class ReservationClientStub:
    def __init__(
        self,
        snapshot: HostawayReservationSnapshot | None = None,
        error: Exception | None = None,
    ) -> None:
        self.snapshot = snapshot
        self.error = error
        self.calls = 0

    def __enter__(self) -> "ReservationClientStub":
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def get_reservation(self, reservation_id: int) -> HostawayReservationSnapshot:
        self.calls += 1
        if self.error:
            raise self.error
        assert self.snapshot is not None
        return self.snapshot

    def create_reservation_with_price_details(self, *args: object, **kwargs: object) -> None:
        raise AssertionError("The webhook path must never create a Hostaway reservation.")

    def update_reservation(self, *args: object, **kwargs: object) -> None:
        raise AssertionError("The webhook path must never update a Hostaway reservation.")

    def cancel_reservation(self, *args: object, **kwargs: object) -> None:
        raise AssertionError("The webhook path must never cancel a Hostaway reservation.")


def snapshot(
    *,
    reservation_id: int = 88001,
    listing_map_id: int = 9001,
    status: str = "new",
    updated_at=None,
    total: Decimal = Decimal("500.25"),
    payment_status: str = "paid",
    source: str = "airbnb",
) -> HostawayReservationSnapshot:
    return HostawayReservationSnapshot(
        reservation_id=reservation_id,
        listing_map_id=listing_map_id,
        channel_id=2001,
        status=status,
        check_in=date(2026, 11, 23),
        check_out=date(2026, 11, 25),
        guests=2,
        currency="SAR",
        total_price=total,
        payment_status=payment_status,
        source=source,
        updated_at=updated_at or timezone.now(),
    )


def received_event(
    *,
    event_type: str = "reservation.created",
    reservation_id: int = 88001,
) -> HostawayWebhookEvent:
    return HostawayWebhookEvent.objects.create(
        event_type=event_type,
        hostaway_reservation_id=reservation_id,
        deduplication_key=f"{reservation_id:064d}",
        body_hash="b" * 64,
        sanitized_payload={
            "event_type": event_type,
            "reservation_id": reservation_id,
        },
    )


def mapped_property(*, map_id: int | None = 9001, listing_id: int = 8001) -> Property:
    property_obj = make_property()
    property_obj.hostaway_listing_id = listing_id
    property_obj.hostaway_listing_map_id = map_id
    property_obj.slug = f"webhook-{listing_id}"
    property_obj.save()
    return property_obj


def test_processor_hydrates_created_reservation_by_map_id() -> None:
    property_obj = mapped_property()
    event = received_event()
    client = ReservationClientStub(snapshot())
    result = process_webhook_event(event.pk, client=client)
    reservation = Reservation.objects.get()
    event.refresh_from_db()
    assert result.code == "processed"
    assert result.match_strategy == "listing_map_id"
    assert reservation.property == property_obj
    assert reservation.booking_intent is None
    assert reservation.source_type == Reservation.SourceType.EXTERNAL_CHANNEL
    assert reservation.normalized_status == Reservation.Status.CONFIRMED
    assert event.status == HostawayWebhookEvent.Status.PROCESSED


def test_updated_before_created_can_create_local_reservation() -> None:
    mapped_property()
    event = received_event(event_type="reservation.updated")
    result = process_webhook_event(
        event.pk, client=ReservationClientStub(snapshot(status="modified"))
    )
    assert result.code == "processed"
    assert Reservation.objects.get().normalized_status == Reservation.Status.MODIFIED


def test_older_event_does_not_overwrite_newer_state() -> None:
    mapped_property()
    newer = timezone.now()
    first_event = received_event()
    process_webhook_event(
        first_event.pk,
        client=ReservationClientStub(snapshot(status="modified", updated_at=newer)),
    )
    second_event = HostawayWebhookEvent.objects.create(
        event_type="reservation.updated",
        hostaway_reservation_id=88001,
        deduplication_key="c" * 64,
        body_hash="d" * 64,
        sanitized_payload={},
    )
    result = process_webhook_event(
        second_event.pk,
        client=ReservationClientStub(
            snapshot(status="cancelled", updated_at=newer - timedelta(days=1))
        ),
    )
    reservation = Reservation.objects.get()
    assert result.code == "stale"
    assert reservation.normalized_status == Reservation.Status.MODIFIED


def test_get_404_is_retryable_without_reservation() -> None:
    event = received_event()
    result = process_webhook_event(
        event.pk,
        client=ReservationClientStub(error=HostawayNotFoundError("synthetic")),
    )
    event.refresh_from_db()
    assert result.code == "retryable"
    assert event.status == HostawayWebhookEvent.Status.RETRYABLE
    assert Reservation.objects.count() == 0


def test_permanent_schema_error_fails_without_retry_loop() -> None:
    event = received_event()
    client = ReservationClientStub(error=HostawayResponseError("synthetic schema error"))
    result = process_webhook_event(event.pk, client=client)
    event.refresh_from_db()
    assert result.code == "failed"
    assert event.status == HostawayWebhookEvent.Status.FAILED
    assert event.error_code == "reservation_schema_invalid"
    assert client.calls == 1
    assert Reservation.objects.count() == 0


def test_listing_id_fallback_and_unmatched_are_safe() -> None:
    property_obj = mapped_property(map_id=None, listing_id=9001)
    fallback_event = received_event()
    fallback = process_webhook_event(
        fallback_event.pk,
        client=ReservationClientStub(snapshot()),
    )
    unmatched_event = HostawayWebhookEvent.objects.create(
        event_type="reservation.created",
        hostaway_reservation_id=88002,
        deduplication_key="e" * 64,
        body_hash="f" * 64,
        sanitized_payload={},
    )
    unmatched = process_webhook_event(
        unmatched_event.pk,
        client=ReservationClientStub(snapshot(reservation_id=88002, listing_map_id=9999)),
    )
    assert fallback.match_strategy == "listing_id_fallback"
    assert Reservation.objects.get(hostaway_reservation_id=88001).property == property_obj
    assert unmatched.match_strategy == "unmatched"
    assert Reservation.objects.get(hostaway_reservation_id=88002).property is None


def test_cancellation_and_financial_changes_are_synced() -> None:
    mapped_property()
    event = received_event()
    process_webhook_event(
        event.pk,
        client=ReservationClientStub(
            snapshot(
                status="cancelled",
                total=Decimal("450.00"),
                payment_status="refunded",
            )
        ),
    )
    reservation = Reservation.objects.get()
    assert reservation.normalized_status == Reservation.Status.CANCELLED
    assert reservation.cancelled_at is not None
    assert reservation.total_price == Decimal("450.0000")
    assert reservation.payment_status == "refunded"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("inquiry", Reservation.Status.INQUIRY),
        ("inquiryPreapproved", Reservation.Status.INQUIRY),
        ("declined", Reservation.Status.DECLINED),
        ("inquiryNotPossible", Reservation.Status.DECLINED),
        ("expired", Reservation.Status.EXPIRED),
        ("inquiryTimeout", Reservation.Status.EXPIRED),
        ("pending", Reservation.Status.PENDING),
        ("awaitingPayment", Reservation.Status.AWAITING_PAYMENT),
    ],
)
def test_lead_statuses_are_recorded_instead_of_unknown(
    raw: str,
    expected: str,
) -> None:
    """These reach real accounts constantly; UNKNOWN would flood admin attention."""
    mapped_property()
    event = received_event()

    process_webhook_event(event.pk, client=ReservationClientStub(snapshot(status=raw)))

    reservation = Reservation.objects.get()
    assert reservation.normalized_status == expected
    assert reservation.hostaway_status == raw


@pytest.mark.parametrize("raw", ["declined", "expired", "inquiryTimeout"])
def test_closed_statuses_end_the_stay_like_a_cancellation(raw: str) -> None:
    mapped_property()

    process_webhook_event(
        received_event().pk,
        client=ReservationClientStub(snapshot(status=raw)),
    )

    reservation = Reservation.objects.get()
    assert reservation.cancelled_at is not None
    assert reservation.confirmed_at is None


def test_a_modified_stay_stays_active() -> None:
    mapped_property()

    process_webhook_event(
        received_event().pk,
        client=ReservationClientStub(snapshot(status="modified")),
    )

    reservation = Reservation.objects.get()
    assert reservation.normalized_status == Reservation.Status.MODIFIED
    assert reservation.confirmed_at is not None
    assert reservation.cancelled_at is None


@pytest.mark.parametrize("source", ["airbnb", "bookingcom"])
def test_other_channels_are_recorded_as_external(source: str) -> None:
    """This Hostaway account also serves other sites. Their bookings are ours to
    observe, never to touch."""
    mapped_property()

    process_webhook_event(
        received_event().pk,
        client=ReservationClientStub(snapshot(status="new", source=source)),
    )

    reservation = Reservation.objects.get()
    assert reservation.source_type == Reservation.SourceType.EXTERNAL_CHANNEL
    assert reservation.booking_intent is None


@pytest.mark.parametrize("source", ["airbnb", "bookingcom", "bookingengine", "manual"])
def test_a_booking_made_elsewhere_cannot_be_opened_by_a_guest_here(source: str) -> None:
    """The booking engine reports as a direct source, so ownership — not the
    source label — has to be what keeps another site's booking out of reach."""
    mapped_property()
    process_webhook_event(
        received_event().pk,
        client=ReservationClientStub(snapshot(status="new", source=source)),
    )
    reservation = Reservation.objects.get()
    assert reservation.booking_intent is None

    response = Client().post(
        "/reservations/manage/",
        {"booking_reference": reservation.public_reference, "email": "guest@example.invalid"},
    )

    assert response.status_code == 400


def test_processing_reads_hostaway_and_never_writes_to_it() -> None:
    """A write from this path would reach the account the other sites share."""
    mapped_property()
    client = ReservationClientStub(snapshot())

    process_webhook_event(received_event().pk, client=client)

    assert client.calls == 1


def test_direct_local_reservation_keeps_booking_intent_link() -> None:
    intent = make_intent(listing_map_id=9001)
    reservation = prepare_local_reservation(intent)
    reservation.hostaway_reservation_id = 88001
    reservation.normalized_status = Reservation.Status.CREATE_UNKNOWN
    reservation.save()
    event = received_event()
    process_webhook_event(event.pk, client=ReservationClientStub(snapshot()))
    reservation.refresh_from_db()
    assert reservation.booking_intent == intent
    assert reservation.source_type == Reservation.SourceType.DIRECT_WEBSITE
    assert Reservation.objects.count() == 1


def test_processing_is_concurrency_safe_for_already_processing_event() -> None:
    event = received_event()
    event.status = HostawayWebhookEvent.Status.PROCESSING
    event.save(update_fields=["status"])
    client = ReservationClientStub(snapshot())
    result = process_webhook_event(event.pk, client=client)
    assert result.code == "not_eligible"
    assert client.calls == 0


def test_processing_command_dry_run_changes_nothing() -> None:
    received_event()
    output = StringIO()
    call_command("process_hostaway_webhooks", dry_run=True, stdout=output)
    event = HostawayWebhookEvent.objects.get()
    assert event.status == HostawayWebhookEvent.Status.RECEIVED
    assert event.attempt_count == 0
    assert Reservation.objects.count() == 0
    assert "Database changes: 0" in output.getvalue()
    assert "Hostaway calls: 0" in output.getvalue()


@override_settings(HOSTAWAY_WEBHOOK_PROCESSING_ENABLED=True)
def test_processing_command_processes_with_mocked_get(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mapped_property()
    event = received_event()
    client = ReservationClientStub(snapshot())
    monkeypatch.setattr(
        "apps.integrations.management.commands.process_hostaway_webhooks.HostawayClient",
        lambda: client,
    )
    output = StringIO()
    call_command("process_hostaway_webhooks", limit=1, stdout=output)
    event.refresh_from_db()
    assert event.status == HostawayWebhookEvent.Status.PROCESSED
    assert Reservation.objects.count() == 1
    assert "processed: 1" in output.getvalue()


def test_failed_event_requires_explicit_retry_flag() -> None:
    event = received_event()
    event.status = HostawayWebhookEvent.Status.FAILED
    event.save(update_fields=["status"])
    client = ReservationClientStub(snapshot())
    assert process_webhook_event(event.pk, client=client).code == "not_eligible"
    result = process_webhook_event(event.pk, client=client, retry_failed=True)
    assert result.code == "processed"
    assert client.calls == 1


def test_webhook_model_has_no_raw_body_field() -> None:
    fields = {field.name for field in HostawayWebhookEvent._meta.fields}
    assert "raw_body" not in fields
    assert "authorization" not in fields
    assert "sanitized_payload" in fields
