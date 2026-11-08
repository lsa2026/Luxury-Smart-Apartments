"""Day-use lifecycle with fake channels: prices, inventory, ownership and isolation."""

from dataclasses import replace
from datetime import datetime, time, timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import uuid4

import pytest
from django.core import signing
from django.test import RequestFactory
from django.urls import reverse
from django.utils import timezone

from apps.integrations.hostaway.reservation_validators import HostawayReservationSnapshot
from apps.payments.day_use_payments import require_payable
from apps.payments.hyperpay.exceptions import HyperPayCheckoutError
from apps.reservations import day_use08_communications as events
from apps.reservations import day_use_hostaway as hostaway
from apps.reservations import day_use_ownership as ownership
from apps.reservations.day_use_stays import (
    ZONE,
    DayUseStayError,
    calculate_price,
    create_stay,
    overnight_conflicts,
    verify_window,
)
from apps.reservations.day_use_views import quote_token, reviewed_quote
from apps.reservations.models import DayUseHostawayOwnership, DayUseStay
from tests.test_availability_service import make_day, make_property
from tests.test_manual_booking_operations_phase61 import owner
from tests.test_reservation_payment_links import billing

pytestmark = pytest.mark.django_db


@pytest.fixture
def rig(settings, monkeypatch):
    settings.DAY_USE08_ENABLED = False
    settings.HYPERPAY_ENVIRONMENT = "production"
    settings.SITE_BASE_URL = "https://testserver"
    settings.HOSTAWAY_LIVE_BOOKING_ENABLED = True
    settings.HOSTAWAY_DIRECT_CHANNEL_ID = 2000
    monkeypatch.setattr("httpx.Client.request", Mock(side_effect=AssertionError("no live request")))
    p = make_property()
    p.hostaway_listing_map_id = 97008
    p.save()
    day = timezone.now().astimezone(ZONE).date() + timedelta(days=2)
    start, end = (datetime.combine(day, time(h), ZONE) for h in (9, 20))
    remote = Mock()
    remote.get_listing_calendar.return_value = SimpleNamespace(
        days=tuple(make_day(day + timedelta(days=i), price=Decimal("943")) for i in (-1, 0, 1))
    )
    remote.get_listing_currency_record.return_value = {
        "id": p.hostaway_listing_id,
        "currencyCode": "SAR",
    }
    remote.guest_stay_page.return_value = []
    actor = owner()
    values = dict(
        actor=actor,
        request_id=uuid4(),
        property_obj=p,
        starts_at=start,
        ends_at=end,
        guest_name="Synthetic Guest",
        guest_phone="+16502530000",
        guest_email="guest@example.test",
        guests=1,
        billing_address=billing(),
        client=remote,
    )
    return SimpleNamespace(
        p=p, start=start, end=end, day=day, remote=remote, actor=actor, values=values
    )


def stay(rig):
    return create_stay(**rig.values)


def snapshot(rig, **changes):
    return replace(
        HostawayReservationSnapshot(
            reservation_id=99808,
            listing_map_id=rig.p.hostaway_listing_map_id,
            channel_id=2000,
            status="new",
            check_in=rig.day - timedelta(days=1),
            check_out=rig.day + timedelta(days=1),
            guests=1,
            currency="SAR",
            total_price=Decimal("740.93"),
            payment_status="",
            source="LSA_DayUse",
            updated_at=None,
        ),
        **changes,
    )


def request(rig):
    r = RequestFactory().post("/")
    r.user = rig.actor
    return r


def test_owner_formula_rounds_only_final_amount(rig):
    amount, hours = calculate_price(943, rig.start, rig.end)
    assert amount == Decimal("740.93") and hours == 11
    assert calculate_price(943, rig.start, rig.start + timedelta(minutes=90))[0] == Decimal(
        "101.04"
    )


def test_freezes_price_and_two_inventory_nights_idempotently(rig):
    s = stay(rig)
    assert s.amount_sar == Decimal("740.93")
    assert s.inventory_arrival_date == rig.day - timedelta(days=1)
    assert s.inventory_departure_date == rig.day + timedelta(days=1)
    assert s.pricing_snapshot["base_price"] == "943"
    assert create_stay(**rig.values).pk == s.pk
    assert DayUseStay.objects.count() == 1
    assert overnight_conflicts(rig.p, rig.day, rig.day + timedelta(days=1))
    assert not overnight_conflicts(rig.p, rig.day + timedelta(days=1), rig.day + timedelta(days=2))


@pytest.mark.parametrize(
    "offset,changes",
    [
        (-1, {"is_available": False}),
        (0, {"is_available": None}),
        (-1, {"closed_on_arrival": True}),
        (1, {"closed_on_departure": True}),
        (0, {"minimum_stay": 3}),
        (0, {"maximum_stay": 1}),
        (0, {"count_available_units": 2}),
        (0, {"count_pending_units": 1}),
        (0, {"count_blocked_units": 1}),
    ],
)
def test_unavailable_or_ambiguous_inventory_never_allocates(rig, offset, changes):
    rows = rig.remote.get_listing_calendar.return_value.days
    rig.remote.get_listing_calendar.return_value.days = tuple(
        replace(row, **changes) if row.date == rig.day + timedelta(days=offset) else row
        for row in rows
    )
    with pytest.raises(DayUseStayError):
        stay(rig)
    assert not DayUseStay.objects.exists()


def test_late_evening_normal_arrival_conflicts_with_inventory(rig):
    rig.remote.guest_stay_page.return_value = [
        dict(
            id=11,
            listingMapId=97008,
            status="new",
            arrivalDate=rig.day.isoformat(),
            departureDate=(rig.day + timedelta(days=1)).isoformat(),
            checkInTime=23,
            checkOutTime=12,
        )
    ]
    with pytest.raises(DayUseStayError, match="ليلتَي"):
        stay(rig)


def test_changed_price_requires_new_manager_review(rig):
    evidence = verify_window(rig.p, rig.start, rig.end, client=rig.remote)
    rig.remote.get_listing_calendar.return_value.days = tuple(
        replace(r, price=Decimal("944")) for r in rig.remote.get_listing_calendar.return_value.days
    )
    with pytest.raises(DayUseStayError, match="تغير السعر"):
        create_stay(**rig.values, expected_pricing=evidence["pricing"])
    assert not DayUseStay.objects.exists()


def test_quote_capability_binds_unit_hours_and_guests(rig):
    pricing = verify_window(rig.p, rig.start, rig.end, client=rig.remote)["pricing"]
    data = dict(
        property=rig.p,
        starts_at=rig.start,
        ends_at=rig.end,
        guests=1,
        quote_token=quote_token(rig.p, rig.start, rig.end, 1, pricing),
    )
    assert reviewed_quote(data) == pricing
    with pytest.raises(DayUseStayError):
        reviewed_quote({**data, "guests": 2})
    with pytest.raises(DayUseStayError):
        reviewed_quote({**data, "quote_token": "bad"})


def test_no_ownership_ack_means_no_hostaway_post(rig, settings, monkeypatch):
    s = stay(rig)
    settings.DAY_USE08_ENABLED = True
    monkeypatch.setattr(ownership, "sync", lambda _: False)
    monkeypatch.setattr(ownership, "queue", lambda _: None)
    with pytest.raises(DayUseStayError, match="ملكية"):
        hostaway.record_in_hostaway(request=request(rig), stay_id=s.pk, client=rig.remote)
    rig.remote.create_reservation_with_price_details.assert_not_called()
    assert DayUseHostawayOwnership.objects.get(stay=s).state == "failed"


def test_confirmed_booking_has_exact_inventory_source_and_no_payment_side_effect(
    rig, settings, monkeypatch
):
    s = stay(rig)
    settings.DAY_USE08_ENABLED = True
    monkeypatch.setattr(ownership, "sync", lambda _: True)
    monkeypatch.setattr(ownership, "queue", lambda _: None)
    monkeypatch.setattr(events, "queue_stay_event", lambda _: None)
    rig.remote.create_reservation_with_price_details.return_value = SimpleNamespace(
        snapshot=snapshot(rig)
    )
    rig.remote.get_reservation.return_value = snapshot(rig)
    identifier = hostaway.record_in_hostaway(request=request(rig), stay_id=s.pk, client=rig.remote)
    assert identifier == 99808
    payload = rig.remote.create_reservation_with_price_details.call_args.args[0]
    assert payload.provider == "LSA_DayUse" and payload.payload["hostNote"].startswith(
        f"DU08:{s.pk}"
    )
    assert payload.payload["departureDate"] == (rig.day + timedelta(days=1)).isoformat()
    assert payload.payload["totalPrice"] == 740.93
    s.refresh_from_db()
    e = s.latest_communication_event()
    assert e.payload["contract_version"] == 2 and e.payload["status"] == "confirmed"
    assert e.payload["arrival_date"] == e.payload["departure_date"] == rig.day.isoformat()
    assert e.payload["inventory_departure_date"] == payload.payload["departureDate"]
    assert not s.attempts.exists() and not s.payment_events.exists() and s.paid_at is None
    hostaway.record_in_hostaway(request=request(rig), stay_id=s.pk, client=rig.remote)
    assert rig.remote.create_reservation_with_price_details.call_count == 1


def test_unknown_creation_never_retries_and_preserves_owned_intent(rig, settings, monkeypatch):
    s = stay(rig)
    settings.DAY_USE08_ENABLED = True
    monkeypatch.setattr(ownership, "sync", lambda _: True)
    monkeypatch.setattr(ownership, "queue", lambda _: None)
    rig.remote.create_reservation_with_price_details.side_effect = TimeoutError
    with pytest.raises(DayUseStayError, match="غير محسومة"):
        hostaway.record_in_hostaway(request=request(rig), stay_id=s.pk, client=rig.remote)
    with pytest.raises(DayUseStayError):
        hostaway.record_in_hostaway(request=request(rig), stay_id=s.pk, client=rig.remote)
    assert rig.remote.create_reservation_with_price_details.call_count == 1
    assert DayUseHostawayOwnership.objects.get(stay=s).state == "unknown"


def test_provider_readback_mismatch_stops_all_guest_events(rig, settings, monkeypatch):
    s = stay(rig)
    settings.DAY_USE08_ENABLED = True
    monkeypatch.setattr(ownership, "sync", lambda _: True)
    monkeypatch.setattr(ownership, "queue", lambda _: None)
    rig.remote.create_reservation_with_price_details.return_value = SimpleNamespace(
        snapshot=snapshot(rig)
    )
    rig.remote.get_reservation.return_value = snapshot(rig, total_price=Decimal("1481.86"))
    with pytest.raises(DayUseStayError, match="مراجعة"):
        hostaway.record_in_hostaway(request=request(rig), stay_id=s.pk, client=rig.remote)
    assert not s.communication_events.exists()
    assert DayUseHostawayOwnership.objects.get(stay=s).provider_reservation_id == 99808


@pytest.mark.parametrize("valid", [False, True])
def test_exact_revision_ownership_ack_and_scoped_read(rig, settings, client, valid):
    s = stay(rig)
    row = DayUseHostawayOwnership.objects.create(stay=s)
    settings.DAY_USE08_ENABLED = True
    settings.DAY_USE08_OWNERSHIP_WEBHOOK_URL = "https://example.test/ownership"
    remote = Mock()
    remote.post.return_value = SimpleNamespace(
        status_code=200,
        json=lambda: dict(
            code="ownership_persisted",
            day_use_stay_id=str(s.pk),
            ownership_revision=1 if valid else 2,
        ),
    )
    assert ownership.sync(s.pk, client=remote) is valid
    response = client.get(
        reverse("integrations:day_use08_ownership", args=[s.pk]),
        HTTP_X_LSA_DAYUSE08_TOKEN=ownership.capability(row),
    )
    assert response.status_code == 200 and "phone" not in response.json()
    wrong = signing.TimestampSigner(salt=events.SALT).sign(str(s.pk))
    assert (
        client.get(
            reverse("integrations:day_use08_ownership", args=[s.pk]),
            HTTP_X_LSA_DAYUSE08_TOKEN=wrong,
        ).status_code
        == 404
    )


def test_checkout_not_released_merely_by_known_signed_link(rig, settings):
    s = stay(rig)
    settings.DAY_USE08_ENABLED = True
    s.availability_evidence[hostaway.KEY] = {"state": "confirmed", "reservation_id": 99808}
    with pytest.raises(HyperPayCheckoutError, match="not_released"):
        require_payable(s)


def test_admin_access_and_page_templates_have_no_side_effect(rig, client, settings):
    import os
    from pathlib import Path

    from tests.test_contact_layout import _standalone

    assert client.get(reverse("notifications:day_use_create")).status_code in {302, 403}
    client.force_login(rig.actor)
    settings.DAY_USE08_ENABLED = True
    r = client.get(reverse("notifications:day_use_create"))
    assert r.status_code == 200 and "فحص الشقق" in r.content.decode()
    if destination := os.environ.get("LSA_DAY_USE_VISUAL_DIR"):
        output = Path(destination)
        output.mkdir(parents=True, exist_ok=True)
        (output / "day-use-create.html").write_text(
            _standalone(r.content.decode()), encoding="utf-8"
        )
    s = stay(rig)
    assert client.get(reverse("notifications:day_use_detail", args=[s.pk])).status_code == 200
    assert not s.attempts.exists()


@pytest.mark.django_db(transaction=True)
def test_concurrent_day_use_allocations_cannot_both_own_inventory(rig):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    from django.db import close_old_connections, connection

    if connection.vendor != "postgresql":
        pytest.skip("Concurrent inventory ownership requires PostgreSQL row locks")
    barrier = Barrier(2)
    calendar = rig.remote.get_listing_calendar.return_value

    def read_calendar(*args, **kwargs):
        barrier.wait(timeout=10)
        return calendar

    rig.remote.get_listing_calendar.side_effect = read_calendar

    def worker():
        close_old_connections()
        try:
            result = create_stay(**{**rig.values, "request_id": uuid4()})
            return str(result.pk)
        except DayUseStayError:
            return "conflict"
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: worker(), range(2)))
    assert results.count("conflict") == 1
    assert DayUseStay.objects.count() == 1
