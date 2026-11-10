"""Synthetic contact matching only; no provider access or marketing delivery."""

import os
from datetime import timedelta
from itertools import count
from pathlib import Path
from uuid import uuid4

import pytest
from django.contrib.auth import get_user_model
from django.db import connection
from django.test import Client
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from apps.reservations.models import DayUseStay, MorningStay
from apps.reservations.services.guest_classification import guest_classifications
from apps.reservations.services.hostaway_booking import prepare_local_reservation
from tests.test_booking_models_services import make_property
from tests.test_hostaway_booking_phase5 import make_intent

pytestmark = pytest.mark.django_db


@pytest.fixture
def factory(settings, monkeypatch):
    settings.HOSTAWAY_WEBSITE_RESERVATION_SYNC_ENABLED = False
    settings.MORNING07_ENABLED = False
    settings.DAY_USE08_ENABLED = False
    monkeypatch.setattr(
        "httpx.Client.request", lambda *args, **kwargs: pytest.fail("no provider IO")
    )
    property_obj = make_property()
    identifiers = count(980001)

    def create(email=None, phone="", **changes):
        identifier = next(identifiers)
        intent = make_intent(property_obj=property_obj)
        intent.guest_email = f"guest{identifier}@example.invalid" if email is None else email
        intent.guest_phone = phone
        intent.save(update_fields=["guest_email", "guest_phone"])
        booking = prepare_local_reservation(intent)
        booking.normalized_status = "confirmed"
        booking.hostaway_reservation_id = identifier
        for name, value in changes.items():
            setattr(booking, name, value)
        booking.save()
        return booking

    return create


@pytest.fixture
def owner_client():
    client = Client()
    client.force_login(get_user_model().objects.create_superuser(username="classification-owner"))
    return client


@pytest.mark.parametrize(
    "first,second,basis",
    [
        (
            (" Guest@Example.invalid ", "+16502530000"),
            ("guest@example.INVALID", "+442079460958"),
            "البريد الإلكتروني",
        ),
        (
            ("first@example.invalid", "0501234567"),
            ("other@example.invalid", "+966 50 123 4567"),
            "رقم الجوال",
        ),
        (
            ("first@example.invalid", "٠٥٠١٢٣٤٥٦٧"),
            ("other@example.invalid", "00966501234567"),
            "رقم الجوال",
        ),
        (
            ("first@example.invalid", "+44 (20) 7946-0958"),
            ("other@example.invalid", "442079460958"),
            "رقم الجوال",
        ),
        (
            ("guest@example.invalid", "+16502530000"),
            ("GUEST@example.invalid", "0016502530000"),
            "البريد والجوال",
        ),
    ],
)
def test_either_normalized_contact_is_enough_for_both_guest_cards(factory, first, second, basis):
    a = factory(email=first[0], phone=first[1])
    b = factory(email=second[0], phone=second[1])
    result = guest_classifications()
    for item in (a, b):
        assert result[item.pk].code == "returning"
        assert basis in result[item.pk].basis


def test_only_exact_contact_matches_count_and_names_do_not(factory):
    a = factory(email="guest+one@example.invalid", phone="+16502530000")
    b = factory(email="guest+two@example.invalid", phone="+442079460958")
    result = guest_classifications()
    assert result[a.pk].code == result[b.pk].code == "new"


@pytest.mark.parametrize(
    "email,phone", [("", ""), ("invalid", "123"), ("same invalid", "00000000")]
)
def test_empty_invalid_contacts_never_match_and_are_not_falsely_new(factory, email, phone):
    a, b = factory(email=email, phone=phone), factory(email=email, phone=phone)
    result = guest_classifications()
    assert result[a.pk].code == result[b.pk].code == "unknown"


@pytest.mark.parametrize(
    "changes",
    [
        {"normalized_status": "cancelled"},
        {"normalized_status": "inquiry"},
        {"normalized_status": "awaiting_payment"},
        {"is_test": True},
        {"normalized_status": "modified", "hostaway_reservation_id": None},
        {"normalized_status": "modified", "hostaway_reservation_id": 0},
    ],
)
def test_cancelled_test_unconfirmed_and_unlinked_rows_do_not_create_returning_guests(
    factory, changes
):
    current = factory(email="match@example.invalid")
    excluded = factory(email="match@example.invalid", **changes)
    result = guest_classifications()
    assert result[current.pk].code == "new"
    assert excluded.pk not in result


def special_stay(model, current, reference, **changes):
    now = timezone.now()
    key = "hostaway_morning" if model is MorningStay else "hostaway_day_use"
    values = dict(
        request_id=uuid4(),
        property=current.property,
        created_by=get_user_model().objects.first(),
        starts_at=now,
        ends_at=now + timedelta(hours=4),
        expires_at=now + timedelta(hours=1),
        availability_checked_at=now,
        environment="production",
        amount_sar="100.00",
        guest_name="Synthetic Guest",
        guest_email=current.booking_intent.guest_email,
        guest_phone=current.booking_intent.guest_phone,
        availability_evidence={key: {"state": "confirmed", "reservation_id": reference}},
    )
    if model is DayUseStay:
        values.update(
            inventory_arrival_date=now.date() - timedelta(days=1),
            inventory_departure_date=now.date() + timedelta(days=1),
        )
    values.update(changes)
    # Bypass lifecycle dispatch deliberately in fixture construction only.
    model.objects.bulk_create([model(**values)])


@pytest.mark.parametrize("model", [MorningStay, DayUseStay])
def test_confirmed_production_special_stays_count_once_by_hostaway_booking(
    factory, owner_client, model
):
    current = factory(email="match@example.invalid")
    special_stay(model, current, current.hostaway_reservation_id)
    assert guest_classifications()[current.pk].code == "new"
    special_stay(model, current, 990099)
    assert guest_classifications()[current.pk].code == "returning"


@pytest.mark.parametrize("model", [MorningStay, DayUseStay])
@pytest.mark.parametrize(
    "changes", [{"environment": "test"}, {"status": "cancelled"}, {"availability_evidence": {}}]
)
def test_unverified_test_or_cancelled_special_stays_are_excluded(
    factory, owner_client, model, changes
):
    current = factory(email="match@example.invalid")
    special_stay(model, current, 990099, **changes)
    assert guest_classifications()[current.pk].code == "new"


def test_search_and_guest_filter_use_full_history_and_render_private_badges(factory, owner_client):
    first = factory(email="match@example.invalid", public_reference="older-search-proof")
    second = factory(email="MATCH@example.invalid", public_reference="newer-search-proof")
    new = factory()
    unknown = factory(email="", phone="")
    url = reverse("notifications:booking_list")
    page = owner_client.get(url, {"q": "newer-search-proof", "guest_type": "returning"})
    assert [b.pk for b in page.context["bookings"]] == [second.pk]
    content = page.content.decode()
    assert content.count("lsa-guest-classification__badge--returning") == 2
    assert "تطابق البريد الإلكتروني" in content
    assert "match@example.invalid" not in content.casefold()
    assert "no-store" in page["Cache-Control"]
    for code, expected in (
        ("returning", {first.pk, second.pk}),
        ("new", {new.pk}),
        ("unknown", {unknown.pk}),
    ):
        page = owner_client.get(url, {"guest_type": code})
        assert {b.pk for b in page.context["bookings"]} == expected
    assert Client().get(url, {"guest_type": "returning"}).status_code == 302


def test_classification_queries_are_read_only_and_do_not_grow_per_booking(factory):
    factory()
    with CaptureQueriesContext(connection) as small:
        guest_classifications()
    for _ in range(12):
        factory()
    with CaptureQueriesContext(connection) as large:
        guest_classifications()
    assert len(small) == len(large) == 3
    assert all(q["sql"].lstrip().upper().startswith("SELECT") for q in large)


def test_duplicates_outside_the_visible_hundred_still_change_guest_classification(
    factory, owner_client
):
    old = factory(email="match@example.invalid")
    for _ in range(100):
        factory()
    latest = factory(email="match@example.invalid")
    page = owner_client.get(reverse("notifications:booking_list"), {"guest_type": "returning"})
    assert {b.pk for b in page.context["bookings"]} == {old.pk, latest.pk}


def test_guest_match_does_not_change_guest_ownership_or_marketing_consent(factory):
    a = factory(email="match@example.invalid")
    b = factory(email="match@example.invalid")
    before = [
        (row.booking_intent.customer_id, row.booking_intent.marketing_consent) for row in (a, b)
    ]
    guest_classifications()
    for row, expected in zip((a, b), before, strict=True):
        row.booking_intent.refresh_from_db()
        assert (row.booking_intent.customer_id, row.booking_intent.marketing_consent) == expected


def test_render_classification_layout(factory, owner_client, tmp_path):
    from tests.test_contact_layout import _standalone

    factory(email="layout@example.invalid")
    factory(email="LAYOUT@example.invalid")
    factory()
    factory(email="", phone="")
    page = owner_client.get(reverse("notifications:booking_list"))
    assert page.status_code == 200
    html = _standalone(page.content.decode())
    for code in ("new", "returning", "unknown"):
        assert f"lsa-guest-classification__badge--{code}" in html
    output = Path(os.environ.get("LSA_CONTACT_VISUAL_DIR", tmp_path))
    output.mkdir(parents=True, exist_ok=True)
    (output / "guest-classification.html").write_text(html, encoding="utf-8")
