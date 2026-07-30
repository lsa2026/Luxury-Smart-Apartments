import json
from datetime import timedelta
from decimal import Decimal
from io import StringIO

import httpx
import pytest
from django.contrib.admin.sites import AdminSite
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import RequestFactory, override_settings
from django.utils import timezone

from apps.integrations.hostaway.client import HostawayClient
from apps.integrations.hostaway.listing_validators import HostawayObjectDocument
from apps.integrations.hostaway.modification_validators import (
    HostawayReservationCancellationRequest,
    HostawayReservationUpdateRequest,
    ReservationIdentifierObservation,
    ReservationObservationDocument,
    validate_reservation_observations,
)
from apps.integrations.hostaway.reservation_validators import ReservationFinanceField
from apps.integrations.hostaway.webhook_processor import sync_reservation_snapshot
from apps.reservations.admin import (
    BookingModificationRequestAdmin,
    HostawayModificationOperationAdmin,
)
from apps.reservations.models import (
    BookingModificationRequest,
    HostawayModificationOperation,
)
from tests.test_booking_modifications_phase6 import (
    confirmed_reservation,
    create_extension,
    updated_snapshot,
)
from tests.test_hostaway_booking_phase5 import TokenStub

pytestmark = pytest.mark.django_db


def update_request() -> HostawayReservationUpdateRequest:
    finance = ReservationFinanceField(
        listing_fee_setting_id=1,
        type="price",
        name="baseRate",
        title="Base rate",
        alias="base",
        quantity=None,
        value=Decimal("650.25"),
        total=Decimal("650.25"),
        is_included_in_total_price=True,
        is_overridden_by_user=False,
        is_mandatory=True,
        is_deleted=False,
    )
    check_in = timezone.localdate() + timedelta(days=20)
    return HostawayReservationUpdateRequest(
        listing_map_id=9001,
        check_in=check_in,
        check_out=check_in + timedelta(days=4),
        guests=2,
        currency="SAR",
        total_price=Decimal("650.25"),
        finance_fields=(finance,),
    )


def reservation_payload(request: HostawayReservationUpdateRequest) -> dict[str, object]:
    return {
        "status": "success",
        "result": {
            "id": 77001,
            "listingMapId": request.listing_map_id,
            "channelId": 2000,
            "status": "modified",
            "arrivalDate": request.check_in.isoformat(),
            "departureDate": request.check_out.isoformat(),
            "numberOfGuests": request.guests,
            "currency": request.currency,
            "totalPrice": str(request.total_price),
            "guestEmail": "private@example.invalid",
            "doorCode": "private",
        },
    }


def test_reservation_observation_validator_discards_pii_values() -> None:
    document = validate_reservation_observations(
        {
            "status": "success",
            "result": [
                {
                    "id": 123456,
                    "listingMapId": 9001,
                    "channelId": 2000,
                    "channelName": "direct",
                    "source": "LuxurySmartApartments",
                    "status": "new",
                    "paymentStatus": "paid",
                    "guestEmail": "private@example.invalid",
                    "phone": "+966500000000",
                    "guestName": "Private Guest",
                }
            ],
        },
        limit=20,
    )
    observation = document.observations[0]
    assert observation.masked_reservation_id == "****3456"
    serialized = repr(observation)
    assert "private@example.invalid" not in serialized
    assert "+966500000000" not in serialized
    assert "Private Guest" not in serialized


@override_settings(
    HOSTAWAY_LIVE_MODIFICATION_ENABLED=True,
    HOSTAWAY_LIVE_EXTENSION_ENABLED=True,
)
def test_client_update_uses_put_without_forbidden_fields_then_get() -> None:
    dto = update_request()
    observed: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        observed.append(request)
        if request.method == "PUT":
            body = request.content.decode()
            assert request.url.path == "/v1/reservations/77001"
            for forbidden in (
                "forceOverbooking",
                "validatePaymentMethod",
                "ccNumber",
                "guestEmail",
                "doorCode",
            ):
                assert forbidden not in str(request.url)
                assert forbidden not in body
            return httpx.Response(200, json=reservation_payload(dto))
        return httpx.Response(200, json=reservation_payload(dto))

    http_client = httpx.Client(
        base_url="https://api.hostaway.com/v1",
        transport=httpx.MockTransport(handler),
    )
    client = HostawayClient(client=http_client, token_provider=TokenStub())
    snapshot = client.update_reservation(77001, dto, extension=True)
    assert snapshot.reservation_id == 77001
    assert [item.method for item in observed] == ["PUT", "GET"]


@override_settings(
    HOSTAWAY_LIVE_CANCELLATION_ENABLED=True,
    BOOKING_AUTOMATIC_CANCELLATION_ENABLED=True,
)
def test_client_cancellation_uses_documented_put_and_reconciles() -> None:
    dto = update_request()
    observed: list[httpx.Request] = []
    response_payload = reservation_payload(dto)
    response_payload["result"]["status"] = "cancelled"

    def handler(request: httpx.Request) -> httpx.Response:
        observed.append(request)
        if request.method == "PUT":
            assert request.url.path == "/v1/reservations/77001/statuses/cancelled"
            assert json.loads(request.content) == {"cancelledBy": "guest"}
        return httpx.Response(200, json=response_payload)

    http_client = httpx.Client(
        base_url="https://api.hostaway.com/v1",
        transport=httpx.MockTransport(handler),
    )
    client = HostawayClient(client=http_client, token_provider=TokenStub())
    snapshot = client.cancel_reservation(
        77001,
        HostawayReservationCancellationRequest(),
    )
    assert snapshot.status == "cancelled"
    assert [item.method for item in observed] == ["PUT", "GET"]


def test_client_write_methods_are_disabled_by_default() -> None:
    client = HostawayClient(
        client=httpx.Client(transport=httpx.MockTransport(lambda request: None)),
        token_provider=TokenStub(),
    )
    with pytest.raises(Exception, match="hostaway_live_modification_disabled"):
        client.update_reservation(77001, update_request())
    with pytest.raises(Exception, match="hostaway_live_cancellation_disabled"):
        client.cancel_reservation(
            77001,
            HostawayReservationCancellationRequest(),
        )


def test_webhook_reconciles_matching_modification_only() -> None:
    reservation = confirmed_reservation()
    modification = create_extension(reservation).request
    assert modification is not None
    modification.status = BookingModificationRequest.Status.UNKNOWN
    modification.save(update_fields=["status"])
    sync_reservation_snapshot(updated_snapshot(modification))
    modification.refresh_from_db()
    assert modification.status == BookingModificationRequest.Status.COMPLETED


def test_webhook_does_not_complete_wrong_modification() -> None:
    reservation = confirmed_reservation()
    modification = create_extension(reservation).request
    assert modification is not None
    modification.status = BookingModificationRequest.Status.UNKNOWN
    modification.save(update_fields=["status"])
    mismatched = replace_snapshot_total(updated_snapshot(modification), Decimal("999.00"))
    sync_reservation_snapshot(mismatched)
    modification.refresh_from_db()
    assert modification.status == BookingModificationRequest.Status.UNKNOWN


def test_webhook_cancellation_completes_local_request_without_refund() -> None:
    reservation = confirmed_reservation()
    modification = create_extension(reservation).request
    assert modification is not None
    modification.request_type = BookingModificationRequest.RequestType.CANCEL_RESERVATION
    modification.new_check_in = None
    modification.new_check_out = None
    modification.new_guests = None
    modification.new_total = None
    modification.status = BookingModificationRequest.Status.UNKNOWN
    modification.save()
    snapshot = updated_snapshot(modification)
    cancelled = snapshot.__class__(
        reservation_id=snapshot.reservation_id,
        listing_map_id=snapshot.listing_map_id,
        channel_id=snapshot.channel_id,
        status="cancelled",
        check_in=reservation.check_in,
        check_out=reservation.check_out,
        guests=reservation.guests,
        currency=reservation.currency,
        total_price=reservation.total_price,
        payment_status="refunded",
        source=snapshot.source,
        updated_at=snapshot.updated_at,
    )
    sync_reservation_snapshot(cancelled)
    modification.refresh_from_db()
    assert modification.status == BookingModificationRequest.Status.COMPLETED


def replace_snapshot_total(snapshot, total):
    return snapshot.__class__(
        reservation_id=snapshot.reservation_id,
        listing_map_id=snapshot.listing_map_id,
        channel_id=snapshot.channel_id,
        status=snapshot.status,
        check_in=snapshot.check_in,
        check_out=snapshot.check_out,
        guests=snapshot.guests,
        currency=snapshot.currency,
        total_price=total,
        payment_status=snapshot.payment_status,
        source=snapshot.source,
        updated_at=snapshot.updated_at,
    )


class IdentifierClientStub:
    def __enter__(self):
        return self

    def __exit__(self, *args):
        return None

    def get_listing_document(self, listing_id, *, include_resources):
        return HostawayObjectDocument(
            record={"id": listing_id, "name": "Synthetic"},
            status="success",
            fields=frozenset({"id", "name"}),
        )

    def retrieve_reservation_observations(self, *, listing_id, limit):
        return ReservationObservationDocument(
            observations=(
                ReservationIdentifierObservation(
                    masked_reservation_id="****0001",
                    reservation_id=88001,
                    listing_map_id=9001,
                    channel_id=2000,
                    channel_name="direct",
                    source="LuxurySmartApartments",
                    status="new",
                    payment_status="paid",
                    field_types=(
                        ("id", "int"),
                        ("listingMapId", "int"),
                        ("channelId", "int"),
                    ),
                ),
            ),
            envelope_field_types=(("result", "list"),),
            count=1,
        )

    def get_reservation(self, reservation_id):
        raise AssertionError("No reservation ID was provided.")


def test_verify_identifier_command_is_read_only(monkeypatch) -> None:
    property_obj = confirmed_reservation().property
    monkeypatch.setattr(
        "apps.reservations.management.commands.verify_hostaway_reservation_identifiers.HostawayClient",
        lambda **kwargs: IdentifierClientStub(),
    )
    output = StringIO()
    call_command(
        "verify_hostaway_reservation_identifiers",
        listing_id=property_obj.hostaway_listing_id,
        reservation_limit=20,
        include_direct_reservations=True,
        show_schema=True,
        strict=True,
        stdout=output,
    )
    report = output.getvalue()
    assert "listing_map_id_verified: true" in report
    assert "direct_channel_id_verified: true" in report
    assert "Hostaway write calls: 0" in report
    assert "private@example.invalid" not in report


def test_verify_modification_command_is_read_only(monkeypatch) -> None:
    property_obj = confirmed_reservation().property
    monkeypatch.setattr(
        "apps.reservations.management.commands.verify_hostaway_modification_prerequisites.HostawayClient",
        lambda **kwargs: IdentifierClientStub(),
    )
    output = StringIO()
    call_command(
        "verify_hostaway_modification_prerequisites",
        listing_id=property_obj.hostaway_listing_id,
        strict=True,
        show_schema=True,
        stdout=output,
    )
    report = output.getvalue()
    assert "documented_update_method: PUT" in report
    assert "documented_cancellation_method: PUT" in report
    assert "Hostaway write calls: 0" in report


def test_modification_admin_is_read_only_and_cannot_complete_manually() -> None:
    user = get_user_model().objects.create_superuser(
        username="phase6-admin",
        email="phase6@example.invalid",
        password="synthetic-password",
    )
    request = RequestFactory().get("/admin/")
    request.user = user
    modification_admin = BookingModificationRequestAdmin(
        BookingModificationRequest,
        AdminSite(),
    )
    operation_admin = HostawayModificationOperationAdmin(
        HostawayModificationOperation,
        AdminSite(),
    )
    assert modification_admin.has_add_permission(request) is False
    assert modification_admin.has_delete_permission(request) is False
    assert "status" in modification_admin.readonly_fields
    assert "quote_snapshot" not in modification_admin.fields
    assert operation_admin.has_add_permission(request) is False
    assert operation_admin.has_delete_permission(request) is False
    assert "request_fingerprint" not in operation_admin.fields
