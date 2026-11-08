"""Manager confirmation creates a morning booking, never a payment or live message."""

from dataclasses import replace
from datetime import timedelta
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from apps.payments.hyperpay.exceptions import HyperPayCheckoutError
from apps.payments.morning_payments import require_payable
from apps.reservations.models import MorningStay
from apps.reservations.morning07_communications import capture_event
from apps.reservations.morning_hostaway import HOSTAWAY_FIRST, booking_state, record_in_hostaway
from apps.reservations.morning_stays import MorningStayError, active_stays, verify_window
from tests.test_morning_hostaway import flags as flags  # noqa: F401
from tests.test_morning_hostaway import paid, request_for
from tests.test_morning_stays import isolated as isolated  # noqa: F401
from tests.test_reservation_payment_links import billing

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def communication_enabled(settings, isolated):  # noqa: F811
    settings.HYPERPAY_ENVIRONMENT = "production"
    settings.MORNING07_N8N_ENABLED = True


def unpaid():
    stay, remote, snapshot = paid()
    MorningStay.objects.filter(pk=stay.pk).update(
        status="open",
        paid_at=None,
        billing_address=billing(),
        availability_evidence={**stay.availability_evidence, "creation_mode": HOSTAWAY_FIRST},
    )
    stay.refresh_from_db()
    return stay, remote, snapshot


def test_confirmed_hostaway_first_emits_once_before_payment_with_complete_guest_data():
    stay, remote, _ = unpaid()
    with pytest.raises(HyperPayCheckoutError):
        require_payable(stay)
    assert not stay.communication_events.exists()
    record_in_hostaway(request=request_for(stay), stay_id=stay.pk, client=remote)
    stay.refresh_from_db()
    assert stay.status == "open" and stay.paid_at is None
    assert not stay.attempts.exists() and not stay.payment_link_deliveries.exists()
    payload = remote.create_reservation_with_price_details.call_args.args[0].to_payload()
    assert payload["phone"] == stay.guest_phone
    assert payload["guestEmail"] == stay.guest_email
    assert payload["guestAddress"] == billing()["billing_street1"]
    assert payload["guestCity"] == billing()["billing_city"]
    assert "Payment pending" in payload["hostNote"]
    event = stay.communication_events.get()
    assert event.payload["status"] == "confirmed"
    assert event.payload["booking_verified"] is True
    assert event.payload["payment_verified"] is False
    assert event.payload["hostaway_reservation_id"] == 99881
    assert event.payload["payment_status"] == "unpaid"
    assert event.payload["check_in_time"] == "01:00"
    assert event.payload["check_out_time"] == "11:00"
    record_in_hostaway(request=request_for(stay), stay_id=stay.pk, client=remote)
    assert remote.create_reservation_with_price_details.call_count == 1
    assert stay.communication_events.count() == 1
    require_payable(stay)


def test_uncertain_creation_never_emits_or_retries_and_retains_occupancy():
    stay, remote, _ = unpaid()
    remote.create_reservation_with_price_details.side_effect = TimeoutError
    with pytest.raises(MorningStayError, match="غير محسومة"):
        record_in_hostaway(request=request_for(stay), stay_id=stay.pk, client=remote)
    stay.refresh_from_db()
    assert booking_state(stay)["state"] == "unknown"
    assert not stay.communication_events.exists()
    MorningStay.objects.filter(pk=stay.pk).update(expires_at=timezone.now() - timedelta(minutes=1))
    assert active_stays(stay.property, stay.starts_at, stay.ends_at).exists()
    with pytest.raises(MorningStayError):
        record_in_hostaway(request=request_for(stay), stay_id=stay.pk, client=remote)
    assert remote.create_reservation_with_price_details.call_count == 1
    with pytest.raises(HyperPayCheckoutError):
        require_payable(stay)


def test_mismatched_response_never_confirms_or_opens_payment():
    stay, remote, snapshot = unpaid()
    remote.create_reservation_with_price_details.return_value.snapshot = replace(
        snapshot, guests=20
    )
    with pytest.raises(MorningStayError):
        record_in_hostaway(request=request_for(stay), stay_id=stay.pk, client=remote)
    stay.refresh_from_db()
    assert booking_state(stay)["state"] == "review"
    assert not stay.communication_events.exists()


def test_payment_change_does_not_repeat_confirmed_booking_messages():
    stay, remote, _ = unpaid()
    record_in_hostaway(request=request_for(stay), stay_id=stay.pk, client=remote)
    MorningStay.objects.filter(pk=stay.pk).update(status="paid", paid_at=timezone.now())
    from django.db import transaction

    with transaction.atomic():
        capture_event(stay.pk)
    assert stay.communication_events.count() == 1


def test_payment_availability_excludes_only_the_matching_linked_hostaway_booking():
    stay, remote, _ = unpaid()
    record_in_hostaway(request=request_for(stay), stay_id=stay.pk, client=remote)
    stay.refresh_from_db()
    own = booking_state(stay)
    row = {
        "id": own["reservation_id"],
        "listingMapId": stay.property.hostaway_listing_map_id,
        "source": "LSA_MorningStay",
        "status": "new",
        "arrivalDate": own["arrival_date"],
        "departureDate": own["departure_date"],
    }
    remote.guest_stay_page.return_value = [row]
    verify_window(stay.property, stay.starts_at, stay.ends_at, client=remote, linked_stay=stay)
    remote.guest_stay_page.return_value = [row, {**row, "id": 99882}]
    with pytest.raises(MorningStayError):
        verify_window(stay.property, stay.starts_at, stay.ends_at, client=remote, linked_stay=stay)
    remote.guest_stay_page.return_value = []
    with pytest.raises(MorningStayError, match="لم يظهر"):
        verify_window(stay.property, stay.starts_at, stay.ends_at, client=remote, linked_stay=stay)
    remote.guest_stay_page.return_value = [{**row, "status": "cancelled"}]
    with pytest.raises(MorningStayError):
        verify_window(stay.property, stay.starts_at, stay.ends_at, client=remote, linked_stay=stay)


def test_create_screen_requires_address_and_manager_confirmation_before_remote_io():
    stay, _, _ = unpaid()
    client = Client()
    client.force_login(stay.created_by)
    url = reverse("notifications:morning_create")
    page = client.get(url)
    assert page.context["form"].fields["billing_city"].required
    assert page.context["form"].fields["confirm_hostaway"].required
    with patch("apps.reservations.morning_views.create_stay") as create:
        assert client.post(url, {}).status_code == 200
        create.assert_not_called()


def test_create_view_confirms_in_hostaway_before_any_payment_or_direct_guest_send():
    stay, remote, _ = unpaid()
    client = Client()
    client.force_login(stay.created_by)
    data = dict(
        request_id=stay.request_id,
        property=stay.property_id,
        date=stay.starts_at.astimezone(ZoneInfo(stay.time_zone)).date(),
        start_time="01:00",
        end_time="11:00",
        guest_name=stay.guest_name,
        guest_phone=stay.guest_phone,
        guest_email=stay.guest_email,
        guest_language=stay.guest_language,
        guests=stay.guests,
        amount_sar=stay.amount_sar,
        confirm_hostaway="on",
        action="confirm_hostaway",
        **billing(),
    )
    with (
        patch("apps.reservations.morning_hostaway.HostawayClient") as factory,
        patch("apps.reservations.morning_payment_messages.UltraMsgClient") as messages,
    ):
        factory.return_value.__enter__.return_value = remote
        result = client.post(reverse("notifications:morning_create"), data)
        assert result.status_code == 302, result.context["form"].errors
        assert client.post(reverse("notifications:morning_create"), data).status_code == 302
        messages.assert_not_called()
    assert remote.create_reservation_with_price_details.call_count == 1
    stay.refresh_from_db()
    assert booking_state(stay)["state"] == "confirmed"
    assert stay.communication_events.count() == 1
    assert not stay.paid_at


def test_checkout_preserves_link_and_does_not_charge_twice_or_reconfirm_booking():
    from tests.test_hyperpay import HyperPayStub
    from tests.test_morning_stays import start_payment

    stay, remote, _ = unpaid()
    record_in_hostaway(request=request_for(stay), stay_id=stay.pk, client=remote)
    stay.refresh_from_db()
    own = booking_state(stay)
    remote.guest_stay_page.return_value = [
        {
            "id": own["reservation_id"],
            "listingMapId": stay.property.hostaway_listing_map_id,
            "source": "LSA_MorningStay",
            "status": "new",
            "arrivalDate": own["arrival_date"],
            "departureDate": own["departure_date"],
        }
    ]
    gateway = HyperPayStub()
    attempt = start_payment(stay, remote, gateway)
    stay.refresh_from_db()
    assert booking_state(stay) == own
    assert stay.availability_evidence["creation_mode"] == HOSTAWAY_FIRST
    assert start_payment(stay, remote, gateway).pk == attempt.pk
    assert gateway.checkout_calls == 1
    assert stay.communication_events.count() == 1
