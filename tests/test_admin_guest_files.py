"""Persistent private reporting, isolated from account ownership and live IO."""

# ruff: noqa: F811 -- pytest fixtures imported for named injection.

import json
import os
from datetime import timedelta
from pathlib import Path
from unittest.mock import Mock

import pytest
from django.core.cache import cache
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from apps.reservations.models import (
    AdminGuestAnchor,
    AdminGuestFile,
    GuestDirectorySnapshot,
    Reservation,
)
from apps.reservations.services import guest_classification_history as history
from apps.reservations.services.guest_classification import guest_classifications
from apps.reservations.services.guest_files import history_for_display, project_history
from tests.test_contact_layout import _standalone
from tests.test_guest_classification import factory as factory
from tests.test_guest_classification import isolated as isolated
from tests.test_guest_classification import owner_client as owner_client
from tests.test_guest_classification import publish as publish
from tests.test_guest_classification import row

pytestmark = pytest.mark.django_db


def stay(
    reference,
    email="shared@example.invalid",
    phone="+966501234567",
    *,
    start="2026-10-01",
    end="2026-10-03",
    channel=2005,
    **extra,
):
    return row(reference, email, phone, channel=channel) | {
        "arrivalDate": start,
        "departureDate": end,
        "totalPrice": "500.25",
        "currency": "SAR",
        **extra,
    }


def sample(factory, publish):
    current = factory(
        email="shared@example.invalid",
        phone="+966501234567",
        source_type=Reservation.SourceType.DIRECT_WEBSITE,
    )
    publish(
        [
            [
                stay(777, email="SHARED@example.invalid", phone="+442079460958", channel=2005),
                stay(
                    778,
                    email="other@example.invalid",
                    phone="00966501234567",
                    start="2026-09-10",
                    end="2026-09-11",
                    channel=2018,
                ),
                stay(
                    current.hostaway_reservation_id,
                    start="2026-11-11",
                    end="2026-11-13",
                    channel=2000,
                ),
                stay(779, email="unrelated@example.invalid", phone="+12025550100"),
            ],
            [],
        ]
    )
    return current, AdminGuestAnchor.objects.get(
        hostaway_reservation_id=current.hostaway_reservation_id
    ).guest_file


def test_permanent_file_contains_direct_or_matches_only_and_no_raw_contacts(
    factory, publish, owner_client
):
    current, file = sample(factory, publish)
    assert file.classification == "returning"
    assert [entry["id"] for entry in file.history] == [current.hostaway_reservation_id, 777, 778]
    assert [entry["nights"] for entry in file.history] == [2, 2, 1]
    assert {row["channel_label"] for row in history_for_display(file)} == {
        "بوكنق",
        "إير بي إن بي",
        "موقعنا",
    }
    stored = json.dumps(GuestDirectorySnapshot.objects.get().data)
    assert "shared@example.invalid" not in stored and "+966501234567" not in stored
    assert "email_key" not in json.dumps(file.history)
    cache.clear()
    assert guest_classifications()[current.pk].code == "returning"
    before = len(history_for_display(file))
    url = reverse("notifications:guest_file", args=[file.pk])
    assert owner_client.get(url).status_code == 200
    assert owner_client.get(url).status_code == 200
    assert len(history_for_display(file)) == before
    assert AdminGuestFile.objects.count() == 1


def test_cache_expiry_and_repeated_visits_never_rescan_saved_source(
    factory, publish, owner_client, isolated
):
    current, file = sample(factory, publish)
    record = GuestDirectorySnapshot.objects.get()
    record.data["completed_at"] = (timezone.now() - timedelta(days=3)).isoformat()
    record.save()
    Reservation.objects.filter(pk=current.pk).update(created_at=timezone.now() - timedelta(days=4))
    cache.clear()
    assert history.ensure_refresh()["status"] == "complete"
    assert not isolated
    for _ in range(2):
        assert (
            owner_client.get(reverse("notifications:guest_file", args=[file.pk])).status_code == 200
        )
        assert owner_client.get(reverse("notifications:booking_list")).status_code == 200
    assert not isolated
    assert guest_classifications()[current.pk].code == "returning"


def test_failed_partial_refresh_retains_last_verified_file(
    factory, publish, owner_client, isolated
):
    _, file = sample(factory, publish)
    saved = file.history, file.checked_at, file.classification
    owner_client.post(reverse("notifications:guest_file", args=[file.pk]), {"action": "refresh"})
    generation, cursor = isolated.pop(0)
    remote = Mock()
    remote.invoice_search_page.side_effect = TimeoutError("no live IO")
    assert history.refresh_page(generation, cursor, client=remote) == "unavailable"
    file.refresh_from_db()
    assert (file.history, file.checked_at, file.classification) == saved
    page = owner_client.get(reverse("notifications:guest_file", args=[file.pk]))
    assert page.context["file"].classification == "returning"
    assert "تعذر التحديث" in page.content.decode()


def test_no_partial_history_is_persisted_before_terminal_page(factory, isolated):
    current = factory()
    history.ensure_refresh()
    generation, cursor = isolated.pop(0)
    remote = Mock()
    remote.invoice_search_page.return_value = [stay(current.hostaway_reservation_id)]
    assert history.refresh_page(generation, cursor, client=remote) == "queued"
    assert not AdminGuestFile.objects.exists() and not GuestDirectorySnapshot.objects.exists()


def test_current_booking_is_managed_by_its_existing_route_and_external_stays_are_not_imported(
    factory, publish, owner_client
):
    current, file = sample(factory, publish)
    redirect = owner_client.get(reverse("notifications:guest_file_for_booking", args=[current.pk]))
    assert redirect.url == reverse("notifications:guest_file", args=[file.pk])
    page = owner_client.get(redirect.url)
    assert reverse("notifications:booking_detail", args=[current.pk]) in page.content.decode()
    assert not Reservation.objects.filter(hostaway_reservation_id__in=[777, 778]).exists()
    current.booking_intent.refresh_from_db()
    assert not current.booking_intent.customer_id and not current.booking_intent.marketing_consent


def test_guest_files_require_operations_access_and_csrf(factory, publish, owner_client):
    current, file = sample(factory, publish)
    urls = [
        reverse("notifications:guest_file", args=[file.pk]),
        reverse("notifications:guest_file_for_booking", args=[current.pk]),
    ]
    for url in urls:
        assert Client().get(url).status_code == 302
        assert Client(enforce_csrf_checks=True).post(url, {"action": "refresh"}).status_code == 403
        assert "no-store" in owner_client.get(url)["Cache-Control"]


def test_identical_national_digits_do_not_cross_country_in_saved_history(factory, publish):
    current = factory(phone="+12025550100")
    publish(
        [
            [
                stay(
                    current.hostaway_reservation_id,
                    email="us@example.invalid",
                    phone="+12025550100",
                ),
                stay(777, email="gb@example.invalid", phone="+442025550100"),
            ],
            [],
        ]
    )
    file = AdminGuestAnchor.objects.get(
        hostaway_reservation_id=current.hostaway_reservation_id
    ).guest_file
    assert len(file.history) == 1 and file.classification == "new"


@pytest.mark.parametrize(
    "extra",
    [
        {"totalPrice": "NaN"},
        {"totalPrice": None},
        {"totalPrice": True},
        {"totalPrice": "-1"},
        {"currency": "invalid"},
        {"arrivalDate": "invalid"},
    ],
)
def test_missing_or_malformed_provider_fields_are_not_fabricated_as_zero(extra):
    projected = project_history(stay(777, **extra), ("email-hmac", "phone-hmac"))
    if "totalPrice" in extra:
        assert projected["total"] is None
    if "currency" in extra:
        assert projected["currency"] == ""
    if "arrivalDate" in extra:
        assert projected["check_in"] is None and projected["nights"] is None


def test_hourly_inventory_block_dates_are_not_reported_as_guest_nights():
    projected = project_history(stay(777, source="LSA_DayUse"), ("", "phone-hmac"))
    assert projected["nights"] is None and projected["check_in"] is None


def test_render_guest_file_layout(factory, publish, owner_client, tmp_path):
    _, file = sample(factory, publish)
    page = owner_client.get(reverse("notifications:guest_file", args=[file.pk]))
    assert page.status_code == 200
    output = Path(os.environ.get("LSA_CONTACT_VISUAL_DIR", tmp_path))
    output.mkdir(parents=True, exist_ok=True)
    (output / "guest-file.html").write_text(_standalone(page.content.decode()), encoding="utf-8")
