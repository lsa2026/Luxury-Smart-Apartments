"""Backdated morning recording: isolated database and fake Hostaway only."""

from dataclasses import replace
from datetime import datetime, time, timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import Mock, patch
from uuid import uuid4

import pytest
from django.contrib.auth.models import AnonymousUser
from django.core.exceptions import PermissionDenied
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from apps.integrations.hostaway.reservation_validators import HostawayReservationSnapshot
from apps.reservations.models import MorningStay
from apps.reservations.morning_hostaway import (
    PROVIDER,
    booking_state,
    cancel_linked_stay,
    record_in_hostaway,
)
from apps.reservations.morning_stays import ZONE, MorningStayError, create_stay
from tests.test_availability_service import make_day
from tests.test_manual_booking_operations_phase61 import owner
from tests.test_morning_stays import create, property_and_remote
from tests.test_morning_stays import isolated as isolated  # noqa: F401

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def flags(settings):
    settings.HOSTAWAY_LIVE_BOOKING_ENABLED = True
    settings.HOSTAWAY_DIRECT_CHANNEL_ID = 2000


def paid():
    p, remote = property_and_remote()
    stay = create(p, remote)
    MorningStay.objects.filter(pk=stay.pk).update(status="paid", paid_at=timezone.now())
    stay.refresh_from_db()
    day = stay.starts_at.astimezone(ZONE).date()
    snapshot = HostawayReservationSnapshot(
        reservation_id=99881,
        listing_map_id=p.hostaway_listing_map_id,
        channel_id=2000,
        status="new",
        check_in=day - timedelta(days=1),
        check_out=day,
        guests=stay.guests,
        currency="SAR",
        total_price=stay.amount_sar,
        payment_status="",
        source=PROVIDER,
        updated_at=None,
    )
    remote.create_reservation_with_price_details.return_value = SimpleNamespace(snapshot=snapshot)
    return stay, remote, snapshot


def request_for(stay):
    return SimpleNamespace(user=stay.created_by, META={})


def test_record_uses_previous_night_explicit_price_and_no_second_charge_or_guest_send():
    stay, remote, _ = paid()
    original_attempts = stay.attempts.count()
    identifier = record_in_hostaway(request=request_for(stay), stay_id=stay.pk, client=remote)
    assert identifier == 99881
    wire = remote.create_reservation_with_price_details.call_args.args[0].to_payload()
    assert (
        wire["arrivalDate"]
        == (stay.starts_at.astimezone(ZONE).date() - timedelta(days=1)).isoformat()
    )
    assert wire["departureDate"] == stay.starts_at.astimezone(ZONE).date().isoformat()
    assert wire["totalPrice"] == 500
    assert wire["financeField"][0]["total"] == 500
    assert wire["financeField"][0]["isOverriddenByUser"] == 1
    assert wire["currency"] == "SAR"
    assert "guestEmail" not in wire and "phone" not in wire
    assert "ACTUAL arrival" in wire["hostNote"] and str(stay.pk) in wire["hostNote"]
    assert not stay.communication_events.exists()
    assert stay.attempts.count() == original_attempts
    assert record_in_hostaway(request=request_for(stay), stay_id=stay.pk, client=remote) == 99881
    assert remote.create_reservation_with_price_details.call_count == 1


def test_0500_immediate_admin_stay_can_be_recorded_as_yesterday_without_public_relaxation():
    p, remote = property_and_remote()
    day = timezone.now().astimezone(ZONE).date()
    now = datetime.combine(day, time(5, 1), ZONE)
    remote.get_listing_calendar.return_value = SimpleNamespace(
        days=tuple(make_day(day + timedelta(days=i)) for i in (-1, 0, 1))
    )
    with patch("django.utils.timezone.now", return_value=now):
        stay = create_stay(
            actor=owner(),
            request_id=uuid4(),
            property_obj=p,
            starts_at=datetime.combine(day, time(5), ZONE),
            ends_at=datetime.combine(day, time(12), ZONE),
            guest_name="Synthetic Guest",
            guest_phone="+16502530000",
            guest_email="morning@example.test",
            guests=1,
            amount_sar=Decimal("500"),
            client=remote,
            allow_started=True,
        )
    assert stay.starts_at.astimezone(ZONE).date() == day
    MorningStay.objects.filter(pk=stay.pk).update(status="paid", paid_at=now)
    snapshot = HostawayReservationSnapshot(
        reservation_id=99882,
        listing_map_id=p.hostaway_listing_map_id,
        channel_id=2000,
        status="new",
        check_in=day - timedelta(days=1),
        check_out=day,
        guests=1,
        currency="SAR",
        total_price=Decimal("500"),
        payment_status="",
        source=PROVIDER,
        updated_at=None,
    )
    remote.create_reservation_with_price_details.return_value = SimpleNamespace(snapshot=snapshot)
    with patch("django.utils.timezone.now", return_value=now):
        assert (
            record_in_hostaway(request=request_for(stay), stay_id=stay.pk, client=remote) == 99882
        )
    wire = remote.create_reservation_with_price_details.call_args.args[0].to_payload()
    assert wire["arrivalDate"] == (day - timedelta(days=1)).isoformat()
    assert wire["departureDate"] == day.isoformat()
    from apps.reservations.services.availability import AvailabilityRequest, AvailabilityService

    result = AvailabilityService._validate_request(
        AvailabilityRequest(property=p, check_in=day - timedelta(days=1), check_out=day, guests=1)
    )
    assert result.code == "past_check_in"


def test_unpaid_or_unauthorized_recording_never_calls_hostaway():
    stay = create()
    remote = Mock()
    with pytest.raises(MorningStayError, match="استلام"):
        record_in_hostaway(request=request_for(stay), stay_id=stay.pk, client=remote)
    with pytest.raises(PermissionDenied):
        record_in_hostaway(
            request=SimpleNamespace(user=AnonymousUser()), stay_id=stay.pk, client=remote
        )
    remote.create_reservation_with_price_details.assert_not_called()


def test_occupied_previous_night_cannot_be_recorded():
    stay, remote, snapshot = paid()
    remote.guest_stay_page.return_value = [
        {
            "id": 999,
            "listingMapId": snapshot.listing_map_id,
            "status": "new",
            "arrivalDate": snapshot.check_in.isoformat(),
            "departureDate": snapshot.check_out.isoformat(),
        }
    ]
    with pytest.raises(MorningStayError, match="خروجه اليوم"):
        record_in_hostaway(request=request_for(stay), stay_id=stay.pk, client=remote)
    remote.create_reservation_with_price_details.assert_not_called()


def test_timeout_is_durable_and_cannot_be_retried_as_another_reservation():
    stay, remote, _ = paid()
    remote.create_reservation_with_price_details.side_effect = TimeoutError()
    for _ in range(2):
        with pytest.raises(MorningStayError):
            record_in_hostaway(request=request_for(stay), stay_id=stay.pk, client=remote)
    stay.refresh_from_db()
    assert booking_state(stay)["state"] == "unknown"
    assert remote.create_reservation_with_price_details.call_count == 1
    assert stay.status == "paid"


def test_changed_price_or_currency_is_retained_for_review_with_provider_id():
    stay, remote, snapshot = paid()
    remote.create_reservation_with_price_details.return_value = SimpleNamespace(
        snapshot=replace(snapshot, total_price=Decimal("777"))
    )
    with pytest.raises(MorningStayError, match="مراجعة"):
        record_in_hostaway(request=request_for(stay), stay_id=stay.pk, client=remote)
    stay.refresh_from_db()
    assert booking_state(stay)["state"] == "review"
    assert booking_state(stay)["reservation_id"] == 99881
    with pytest.raises(MorningStayError):
        record_in_hostaway(request=request_for(stay), stay_id=stay.pk, client=remote)
    assert remote.create_reservation_with_price_details.call_count == 1


def test_cancel_linked_stay_requires_native_confirmation_and_never_refunds():
    stay, remote, snapshot = paid()
    record_in_hostaway(request=request_for(stay), stay_id=stay.pk, client=remote)
    remote.get_reservation.return_value = snapshot
    remote.cancel_morning_reservation.return_value = replace(snapshot, status="cancelled")
    result = cancel_linked_stay(stay.pk, client=remote)
    assert result.status == "cancelled" and result.paid_at
    assert booking_state(result)["state"] == "cancelled"
    assert remote.cancel_morning_reservation.call_count == 1
    assert not result.attempts.exists()


def test_uncertain_native_cancellation_cannot_report_local_success():
    stay, remote, snapshot = paid()
    record_in_hostaway(request=request_for(stay), stay_id=stay.pk, client=remote)
    remote.get_reservation.return_value = snapshot
    remote.cancel_morning_reservation.side_effect = TimeoutError()
    with pytest.raises(MorningStayError):
        cancel_linked_stay(stay.pk, client=remote)
    stay.refresh_from_db()
    assert stay.status == "paid"
    assert booking_state(stay)["state"] == "cancel_unknown"


def test_admin_record_action_is_post_only_and_guest_cannot_call_it():
    stay, _, _ = paid()
    url = reverse("notifications:morning_detail", args=[stay.pk])
    browser = Client()
    assert browser.post(url, {"action": "record_hostaway"}).status_code == 302
    browser.force_login(stay.created_by)
    with patch("apps.reservations.morning_views.record_in_hostaway", return_value=99881) as send:
        assert browser.get(url).status_code == 200
        send.assert_not_called()
        assert browser.post(url, {"action": "record_hostaway"}).status_code == 302
        assert send.call_count == 1


def test_explicit_cancel_boundary_rejects_other_sources_and_does_not_enable_automatic_cancels():
    from apps.integrations.hostaway.client import HostawayClient
    from apps.integrations.hostaway.exceptions import HostawayConfigurationError

    _, _, snapshot = paid()
    remote = SimpleNamespace(
        get_reservation=Mock(return_value=replace(snapshot, source="other")),
        _put_json=Mock(),
    )
    with pytest.raises(HostawayConfigurationError, match="not_a_morning"):
        HostawayClient.cancel_morning_reservation(remote, 99881)
    remote._put_json.assert_not_called()


@pytest.mark.django_db(transaction=True)
def test_parallel_recording_has_one_provider_post():
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    from django.db import close_old_connections, connection

    if connection.vendor != "postgresql":
        pytest.skip("Row-lock regression requires PostgreSQL.")
    stay, remote, _ = paid()
    gate = Barrier(2)

    def verified(*args, **kwargs):
        gate.wait(timeout=10)
        return {"source": "hostaway_read_only"}

    def run():
        close_old_connections()
        try:
            return record_in_hostaway(request=request_for(stay), stay_id=stay.pk, client=remote)
        except MorningStayError:
            return "already_inflight"
        finally:
            close_old_connections()

    with patch("apps.reservations.morning_hostaway.verify_window", side_effect=verified):
        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(lambda _: run(), range(2)))
    assert 99881 in outcomes
    assert remote.create_reservation_with_price_details.call_count == 1


def test_different_payment_environment_is_not_recorded(settings):
    stay, remote, _ = paid()
    settings.HYPERPAY_ENVIRONMENT = "another-environment"
    with pytest.raises(MorningStayError, match="بيئة"):
        record_in_hostaway(request=request_for(stay), stay_id=stay.pk, client=remote)
    remote.create_reservation_with_price_details.assert_not_called()
