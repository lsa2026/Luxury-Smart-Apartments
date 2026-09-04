"""The Listing Map ID backfill and the alert for bookings it would unblock."""

from datetime import timedelta
from io import StringIO

import pytest
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import RequestFactory
from django.utils import timezone

from apps.core.admin_dashboard import dashboard_payload
from apps.integrations.hostaway.modification_validators import (
    ReservationIdentifierObservation,
    ReservationObservationDocument,
)
from apps.properties.management.commands import backfill_hostaway_listing_map_ids
from apps.properties.models import Property
from apps.reservations.models import Reservation
from tests.test_booking_models_services import make_property

pytestmark = pytest.mark.django_db


def superuser_payload() -> dict:
    user = get_user_model().objects.create_superuser(
        username="dashboard-admin",
        email="dashboard-admin@example.invalid",
        password="Correct-Horse-Battery-2026",
    )
    request = RequestFactory().get("/admin/")
    request.user = user
    return dashboard_payload(request)


def observation(listing_map_id: int | None) -> ReservationIdentifierObservation:
    return ReservationIdentifierObservation(
        masked_reservation_id="****1234",
        reservation_id=91001,
        listing_map_id=listing_map_id,
        channel_id=2000,
        channel_name="direct",
        source="apiv1",
        status="new",
        payment_status="Paid",
        field_types=(),
    )


class ClientStub:
    """Stands in for HostawayClient; records which listings were asked about."""

    def __init__(self, by_listing: dict[int, list[int | None]]) -> None:
        self.by_listing = by_listing
        self.asked: list[int] = []

    def __enter__(self) -> "ClientStub":
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def retrieve_reservation_observations(
        self,
        *,
        listing_id: int,
        limit: int = 20,
    ) -> ReservationObservationDocument:
        self.asked.append(listing_id)
        values = self.by_listing.get(listing_id, [])
        return ReservationObservationDocument(
            observations=tuple(observation(value) for value in values),
            envelope_field_types=(),
            count=len(values),
        )


def use_stub(monkeypatch: pytest.MonkeyPatch, stub: ClientStub) -> None:
    monkeypatch.setattr(
        backfill_hostaway_listing_map_ids,
        "HostawayClient",
        lambda **kwargs: stub,
    )


def property_without_map_id(listing_id: int, slug: str) -> Property:
    property_obj = make_property()
    property_obj.hostaway_listing_id = listing_id
    property_obj.hostaway_listing_map_id = None
    property_obj.slug = slug
    property_obj.save()
    return property_obj


def test_backfill_stores_the_value_the_account_reports(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    property_obj = property_without_map_id(315814, "backfill-one")
    stub = ClientStub({315814: [315814, 315814]})
    use_stub(monkeypatch, stub)

    call_command("backfill_hostaway_listing_map_ids", stdout=StringIO())

    property_obj.refresh_from_db()
    assert property_obj.hostaway_listing_map_id == 315814
    assert property_obj.hostaway_listing_map_id_verified_at is not None
    assert (
        property_obj.hostaway_listing_map_id_verification_source
        == backfill_hostaway_listing_map_ids.VERIFICATION_SOURCE
    )


def test_dry_run_writes_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    property_obj = property_without_map_id(315815, "backfill-dry")
    use_stub(monkeypatch, ClientStub({315815: [315815]}))
    output = StringIO()

    call_command("backfill_hostaway_listing_map_ids", "--dry-run", stdout=output)

    property_obj.refresh_from_db()
    assert property_obj.hostaway_listing_map_id is None
    assert "dry run" in output.getvalue()


def test_conflicting_values_are_left_for_a_human(monkeypatch: pytest.MonkeyPatch) -> None:
    """A multi-unit listing must never be guessed; the wrong unit would be booked."""
    property_obj = property_without_map_id(325731, "backfill-conflict")
    use_stub(monkeypatch, ClientStub({325731: [325731, 999999]}))
    output = StringIO()

    call_command("backfill_hostaway_listing_map_ids", stdout=output)

    property_obj.refresh_from_db()
    assert property_obj.hostaway_listing_map_id is None
    assert "conflicting values" in output.getvalue()


def test_listings_without_reservations_are_reported_not_invented(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    property_obj = property_without_map_id(325961, "backfill-empty")
    use_stub(monkeypatch, ClientStub({325961: []}))
    output = StringIO()

    call_command("backfill_hostaway_listing_map_ids", stdout=output)

    property_obj.refresh_from_db()
    assert property_obj.hostaway_listing_map_id is None
    assert "no reservation reported" in output.getvalue()


def test_properties_that_already_have_an_id_are_not_re_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    property_obj = property_without_map_id(343666, "backfill-known")
    property_obj.hostaway_listing_map_id = 343666
    property_obj.save()
    stub = ClientStub({343666: [111111]})
    use_stub(monkeypatch, stub)

    call_command("backfill_hostaway_listing_map_ids", stdout=StringIO())

    property_obj.refresh_from_db()
    assert stub.asked == []
    assert property_obj.hostaway_listing_map_id == 343666


def test_sample_size_is_validated() -> None:
    with pytest.raises(CommandError):
        call_command("backfill_hostaway_listing_map_ids", "--sample", "0")


def test_a_paid_booking_stuck_before_hostaway_raises_an_alert() -> None:
    reservation = Reservation.objects.create(
        property=make_property(),
        normalized_status=Reservation.Status.READY_FOR_HOSTAWAY,
        payment_status="paid",
        check_in=timezone.localdate() + timedelta(days=9),
        check_out=timezone.localdate() + timedelta(days=11),
        nights=2,
        guests=2,
        currency="SAR",
        total_price="900.0000",
    )
    Reservation.objects.filter(pk=reservation.pk).update(
        updated_at=timezone.now() - timedelta(minutes=30)
    )

    context = superuser_payload()

    assert context["stale_unsent_reservations"] == 1
    # Matched by destination, not by label: the label is translated.
    alert = next(
        item
        for item in context["action_queue"]
        if item["url"].endswith("/reservations/hostawayreservationoperation/")
    )
    assert alert["severity"] == "critical"
    assert alert["count"] == 1


def test_a_booking_still_on_its_way_to_hostaway_is_not_an_alert() -> None:
    Reservation.objects.create(
        property=make_property(),
        normalized_status=Reservation.Status.READY_FOR_HOSTAWAY,
        payment_status="paid",
        check_in=timezone.localdate() + timedelta(days=9),
        check_out=timezone.localdate() + timedelta(days=11),
        nights=2,
        guests=2,
        currency="SAR",
        total_price="900.0000",
    )

    assert superuser_payload()["stale_unsent_reservations"] == 0
