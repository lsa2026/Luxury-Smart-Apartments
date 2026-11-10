"""All-channel Hostaway classification with synthetic provider pages only."""

import json
import os
from datetime import timedelta
from itertools import count
from pathlib import Path
from unittest.mock import Mock

import pytest
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.db import connection
from django.test import Client
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from apps.integrations.hostaway.client import HostawayClient
from apps.reservations.models import Reservation
from apps.reservations.services import guest_classification_history as history
from apps.reservations.services.guest_classification import guest_classifications
from apps.reservations.services.hostaway_booking import prepare_local_reservation
from tests.test_booking_models_services import make_property
from tests.test_hostaway_booking_phase5 import make_intent

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def isolated(monkeypatch, settings):
    cache.clear()
    settings.HOSTAWAY_WEBSITE_RESERVATION_SYNC_ENABLED = False
    monkeypatch.setattr(history, "shared_cache_available", lambda: True)
    queued = []
    monkeypatch.setattr(
        history, "dispatch", lambda generation, cursor: queued.append((generation, cursor))
    )
    monkeypatch.setattr(
        "httpx.Client.request", lambda *args, **kwargs: pytest.fail("no live provider IO")
    )
    yield queued
    cache.clear()


@pytest.fixture
def factory():
    property_obj, identifiers = make_property(), count(980001)

    def create(email=None, phone="", **changes):
        identifier = next(identifiers)
        intent = make_intent(property_obj=property_obj)
        intent.guest_email = f"guest{identifier}@example.invalid" if email is None else email
        intent.guest_phone = phone
        intent.save(update_fields=["guest_email", "guest_phone"])
        booking = prepare_local_reservation(intent)
        booking.normalized_status, booking.hostaway_reservation_id = "confirmed", identifier
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


def row(reference, email="other@example.invalid", phone="", status="new", channel=2002):
    return {
        "id": reference,
        "guestEmail": email,
        "phone": phone,
        "status": status,
        "channelId": channel,
    }


@pytest.fixture
def publish(isolated):
    def complete(pages):
        history.ensure_refresh(force=True)
        remote = Mock()
        remote.invoice_search_page.side_effect = pages
        for _ in range(20):
            if not isolated:
                break
            generation, cursor = isolated.pop(0)
            history.refresh_page(generation, cursor, client=remote)
        assert history.provider_state()["status"] == "complete"
        return remote

    return complete


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
def test_booking_com_or_other_external_booking_matches_either_contact(
    factory, publish, first, second, basis
):
    current = factory(email=second[0], phone=second[1])
    publish([[row(777, *first, channel=2002), row(current.hostaway_reservation_id, *second)], []])
    result = guest_classifications()[current.pk]
    assert result.code == "returning" and basis in result.basis
    assert "هوستاوي" in result.basis
    assert not Reservation.objects.filter(hostaway_reservation_id=777).exists()


def test_local_duplicate_cannot_replace_hostaway_evidence(factory, publish):
    current = factory(email="match@example.invalid")
    factory(email="match@example.invalid")
    publish([[row(current.hostaway_reservation_id, "match@example.invalid")], []])
    assert guest_classifications()[current.pk].code == "new"


def test_current_hostaway_booking_alone_is_not_a_return(factory, publish):
    current = factory()
    publish([[row(current.hostaway_reservation_id, current.booking_intent.guest_email)], []])
    assert guest_classifications()[current.pk].code == "new"


def test_current_identity_comes_from_hostaway_not_stale_site_contacts(factory, publish):
    current = factory(email="stale@example.invalid")
    publish(
        [
            [
                row(current.hostaway_reservation_id, "current@example.invalid"),
                row(777, "current@example.invalid"),
            ],
            [],
        ]
    )
    assert guest_classifications()[current.pk].code == "returning"


def test_local_email_cannot_override_different_hostaway_identity(factory, publish):
    current = factory(email="old@example.invalid")
    publish(
        [
            [
                row(current.hostaway_reservation_id, "current@example.invalid"),
                row(777, "old@example.invalid"),
            ],
            [],
        ]
    )
    assert guest_classifications()[current.pk].code == "new"


def test_missing_shared_cache_never_queues_or_claims_new(factory, isolated, monkeypatch):
    current = factory()
    monkeypatch.setattr(history, "shared_cache_available", lambda: False)
    assert history.ensure_refresh()["status"] == "unavailable"
    assert not isolated
    assert guest_classifications()[current.pk].code == "unknown"


def test_repeated_page_visits_share_one_job_and_stalled_job_is_replaced(factory, isolated):
    factory()
    history.ensure_refresh()
    history.ensure_refresh()
    assert len(isolated) == 1
    generation, cursor = isolated[0]
    key = f"{history.PREFIX}:job:{generation}"
    job = cache.get(key)
    job["updated_at"] = (timezone.now() - timedelta(minutes=2)).isoformat()
    cache.set(key, job)
    history.ensure_refresh()
    assert len(isolated) == 2
    assert history.refresh_page(generation, cursor, client=Mock()) == "obsolete"


def test_new_requires_terminal_page_and_full_all_channel_cursor_scan(factory, publish):
    current = factory(email="match@example.invalid")
    remote = publish(
        [
            [row(current.hostaway_reservation_id, "match@example.invalid")],
            [row(777, "MATCH@example.invalid", channel=1)],
            [],
        ]
    )
    assert guest_classifications()[current.pk].code == "returning"
    assert [call.kwargs for call in remote.invoice_search_page.call_args_list] == [
        {"after_id": None},
        {"after_id": current.hostaway_reservation_id},
        {"after_id": 777},
    ]


@pytest.mark.parametrize("status", ["cancelled", "inquiry", "pending", "declined", "expired"])
def test_non_booking_provider_statuses_are_excluded(factory, publish, status):
    current = factory(email="match@example.invalid")
    publish(
        [
            [
                row(current.hostaway_reservation_id, "match@example.invalid"),
                row(777, "match@example.invalid", status=status),
            ],
            [],
        ]
    )
    assert guest_classifications()[current.pk].code == "new"


@pytest.mark.parametrize(
    "email,phone", [("", ""), ("invalid", "123"), ("same invalid", "00000000")]
)
def test_invalid_or_empty_contacts_are_unknown(factory, publish, email, phone):
    current = factory(email=email, phone=phone)
    publish([[row(777, email, phone), row(current.hostaway_reservation_id, email, phone)], []])
    assert guest_classifications()[current.pk].code == "unknown"


def test_incomplete_or_failed_history_never_labels_guest_new(factory, isolated):
    current = factory(email="match@example.invalid")
    history.ensure_refresh()
    generation, cursor = isolated.pop(0)
    remote = Mock()
    remote.invoice_search_page.return_value = [row(777, "match@example.invalid")]
    assert history.refresh_page(generation, cursor, client=remote) == "queued"
    assert history.provider_snapshot() is None
    assert guest_classifications()[current.pk].code == "unknown"
    generation, cursor = isolated.pop(0)
    remote.invoice_search_page.side_effect = TimeoutError("private contact must not be logged")
    assert history.refresh_page(generation, cursor, client=remote) == "unavailable"
    assert history.provider_state()["failure_code"] == "source_unavailable"
    assert "private contact" not in json.dumps(history.provider_state())
    assert guest_classifications()[current.pk].code == "unknown"


@pytest.mark.parametrize(
    "pages",
    [
        [[row(1)], [row(1)]],
        [[row(True)]],
        [[row(1), row(1)]],
        [[row(1, status="unsupported")]],
        [[row(1, email={"private": "invalid"})]],
        [[row(i + 1) for i in range(51)]],
    ],
)
def test_repeated_malformed_or_unbounded_provider_pages_are_not_complete(factory, isolated, pages):
    current = factory()
    history.ensure_refresh()
    remote = Mock()
    remote.invoice_search_page.side_effect = pages
    while isolated:
        generation, cursor = isolated.pop(0)
        history.refresh_page(generation, cursor, client=remote)
    assert history.provider_snapshot() is None
    assert guest_classifications()[current.pk].code == "unknown"


def test_page_limit_does_not_publish_a_truncated_scan(factory, isolated, monkeypatch):
    current = factory()
    monkeypatch.setattr(history, "MAX_PAGES", 1)
    history.ensure_refresh()
    remote = Mock()
    remote.invoice_search_page.return_value = [row(777)]
    generation, cursor = isolated.pop(0)
    assert history.refresh_page(generation, cursor, client=remote) == "unavailable"
    assert guest_classifications()[current.pk].code == "unknown"


def test_duplicate_tasks_and_busy_workers_do_not_repeat_the_page(factory, isolated):
    factory()
    history.ensure_refresh()
    generation, cursor = isolated.pop(0)
    remote = Mock()
    remote.invoice_search_page.return_value = [row(777)]
    cache.set(history.PREFIX + ":lock", "other-worker", timeout=45)
    assert history.refresh_page(generation, cursor, client=remote) == "busy"
    remote.invoice_search_page.assert_not_called()
    cache.delete(history.PREFIX + ":lock")
    assert history.refresh_page(generation, cursor, client=remote) == "queued"
    assert history.refresh_page(generation, cursor, client=remote) == "obsolete"
    assert remote.invoice_search_page.call_count == 1


def test_only_keyed_contacts_are_cached_and_verified_results_are_permanent(factory, publish):
    current = factory(email="private@example.invalid", phone="+16502530000")
    publish(
        [
            [
                row(777, current.booking_intent.guest_email, current.booking_intent.guest_phone),
                row(
                    current.hostaway_reservation_id,
                    current.booking_intent.guest_email,
                    current.booking_intent.guest_phone,
                ),
            ],
            [],
        ]
    )
    snapshot = cache.get(history.SNAPSHOT)
    encoded = json.dumps(snapshot)
    assert "private@example.invalid" not in encoded and "+16502530000" not in encoded
    assert guest_classifications()[current.pk].code == "returning"
    snapshot["completed_at"] = (timezone.now() - timedelta(minutes=16)).isoformat()
    cache.set(history.SNAPSHOT, snapshot)
    assert guest_classifications()[current.pk].code == "returning"
    cache.clear()
    assert guest_classifications()[current.pk].code == "returning"


def test_cache_loss_or_wrong_worker_key_is_not_new(factory, publish, monkeypatch):
    current = factory()
    publish([[]])
    snapshot = cache.get(history.SNAPSHOT)
    snapshot["key_version"] = "different-worker-key"
    cache.set(history.SNAPSHOT, snapshot)
    assert guest_classifications()[current.pk].code == "unknown"
    monkeypatch.setattr(cache, "get", Mock(side_effect=RuntimeError("cache unavailable")))
    assert history.ensure_refresh()["status"] == "unavailable"
    assert guest_classifications()[current.pk].code == "unknown"


def test_search_filters_and_private_status_api_use_hostaway_snapshot(
    factory, publish, owner_client
):
    returning = factory(email="match@example.invalid", public_reference="source-proof")
    new = factory()
    unknown = factory(email="", phone="")
    publish(
        [
            [
                row(777, "match@example.invalid", channel=2002),
                row(returning.hostaway_reservation_id, "match@example.invalid"),
                row(new.hostaway_reservation_id, new.booking_intent.guest_email),
            ],
            [],
        ]
    )
    url = reverse("notifications:booking_list")
    page = owner_client.get(url, {"q": "source-proof", "guest_type": "returning"})
    assert [b.pk for b in page.context["bookings"]] == [returning.pk]
    assert page.content.count(b"lsa-guest-classification__badge--returning") == 2
    assert "match@example.invalid" not in page.content.decode().casefold()
    assert "no-store" in page["Cache-Control"]
    for code, expected in (
        ("returning", {returning.pk}),
        ("new", {new.pk}),
        ("unknown", {unknown.pk}),
    ):
        assert {
            b.pk for b in owner_client.get(url, {"guest_type": code}).context["bookings"]
        } == expected
    status = owner_client.get(url, {"classification_status": 1})
    assert status.json()["status"] == "complete"
    assert Client().get(url, {"classification_status": 1}).status_code == 302
    private = Client(enforce_csrf_checks=True)
    private.force_login(get_user_model().objects.get(username="classification-owner"))
    assert private.post(url, {"action": "refresh_guest_classification"}).status_code == 403


def test_pending_source_is_not_presented_as_an_empty_guest_segment(factory, owner_client):
    factory()
    page = owner_client.get(reverse("notifications:booking_list"), {"guest_type": "new"})
    assert page.context["bookings"] == []
    assert "لم يكتمل تصنيف الضيوف" in page.content.decode()
    assert "لا توجد حجوزات مؤكدة في هوستاوي مطابقة" not in page.content.decode()


def test_all_channel_api_boundary_has_no_email_phone_channel_or_date_filter():
    client = object.__new__(HostawayClient)
    client._get_json = Mock(return_value={"status": "success", "result": [row(777)]})
    assert client.invoice_search_page(after_id=500)[0]["id"] == 777
    client._get_json.assert_called_once_with(
        "/reservations", params=[("limit", 50), ("includeResources", 0), ("afterId", 500)]
    )


def test_render_classification_layout(factory, publish, owner_client, tmp_path):
    from tests.test_contact_layout import _standalone

    first = factory(email="layout@example.invalid")
    second = factory(email="LAYOUT@example.invalid")
    new = factory()
    factory(email="", phone="")
    publish(
        [
            [
                row(777, "layout@example.invalid", channel=2002),
                row(first.hostaway_reservation_id, "layout@example.invalid"),
                row(second.hostaway_reservation_id, "layout@example.invalid"),
                row(new.hostaway_reservation_id, new.booking_intent.guest_email),
            ],
            [],
        ]
    )
    page = owner_client.get(reverse("notifications:booking_list"))
    assert page.status_code == 200
    html = _standalone(page.content.decode())
    for code in ("new", "returning", "unknown"):
        assert f"lsa-guest-classification__badge--{code}" in html
    output = Path(os.environ.get("LSA_CONTACT_VISUAL_DIR", tmp_path))
    output.mkdir(parents=True, exist_ok=True)
    (output / "guest-classification.html").write_text(html, encoding="utf-8")


def test_empty_or_different_account_history_cannot_certify_a_guest_new(factory, publish):
    current = factory()
    publish([[]])
    assert guest_classifications()[current.pk].code == "unknown"


def test_classification_is_read_only_and_does_not_link_accounts_or_opt_guests_in(factory, publish):
    current = factory()
    publish([[]])
    before = current.booking_intent.customer_id, current.booking_intent.marketing_consent
    with CaptureQueriesContext(connection) as queries:
        guest_classifications()
    assert len(queries) == 1
    sql = queries[0]["sql"].lstrip().upper()
    assert sql.startswith("SELECT") or (sql.startswith("DECLARE ") and " FOR SELECT " in sql)
    current.booking_intent.refresh_from_db()
    assert (current.booking_intent.customer_id, current.booking_intent.marketing_consent) == before
