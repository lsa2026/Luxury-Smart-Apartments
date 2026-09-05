from unittest.mock import Mock, patch

import pytest
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import Client, override_settings
from django.urls import reverse

from apps.integrations.hostaway.property_services import sync_properties
from apps.integrations.models import IntegrationSyncRun
from apps.integrations.tasks import (
    distributed_task_lock,
    expire_booking_objects_task,
    reconcile_paid_hostaway_reservations_task,
    sync_hostaway_properties_task,
    sync_hostaway_reviews_task,
)
from apps.payments.models import PaymentAttempt
from apps.properties.models import Property
from apps.properties.services.publishing import evaluate_listing_publish_readiness
from apps.reservations.models import Reservation

pytestmark = pytest.mark.django_db


class FakeListingsClient:
    def __init__(self, pages: list[tuple[list[dict[str, object]], int | None]]) -> None:
        self.pages = list(pages)

    def get_listings(self, **kwargs: object) -> tuple[list[dict[str, object]], int | None]:
        return self.pages.pop(0)

    def get_amenities(self) -> list[dict[str, object]]:
        return []


def listing_payload(listing_id: int = 71001) -> dict[str, object]:
    return {
        "id": listing_id,
        "name": f"Listing {listing_id}",
        "personCapacity": 4,
        "currencyCode": "SAR",
        "city": "Riyadh",
        "specialStatus": None,
        "listingImages": [
            {
                "id": listing_id * 10,
                "url": f"https://hostaway-platform.s3.us-west-2.amazonaws.com/{listing_id}.jpg",
                "sortOrder": 1,
            }
        ],
        "listingAmenities": [],
    }


def run_full_sync(*payloads: dict[str, object]) -> object:
    return sync_properties(
        client=FakeListingsClient([(list(payloads), len(payloads))]),
        include_amenities=False,
    )


def test_complete_new_listing_is_automatically_published() -> None:
    report = run_full_sync(listing_payload())
    property_obj = Property.objects.get()

    assert property_obj.is_visible is True
    assert property_obj.visibility_management == Property.VisibilityManagement.AUTOMATIC
    assert property_obj.publish_blockers == []
    assert report.properties_auto_published == 1


@override_settings(HOSTAWAY_LIVE_BOOKING_ENABLED=True)
def test_live_listing_without_map_id_is_not_automatically_published() -> None:
    report = run_full_sync(listing_payload())
    property_obj = Property.objects.get()

    assert property_obj.is_visible is False
    assert "missing_listing_map_id" in property_obj.publish_blockers
    assert report.properties_pending_review == 1


@pytest.mark.parametrize(
    ("field", "value", "blocker"),
    [
        ("listingImages", [], "missing_visible_image"),
        ("personCapacity", None, "missing_capacity"),
        ("currencyCode", "", "missing_currency"),
    ],
)
def test_incomplete_new_listing_waits_for_review(
    field: str,
    value: object,
    blocker: str,
) -> None:
    payload = listing_payload()
    payload[field] = value

    report = run_full_sync(payload)
    property_obj = Property.objects.get()

    assert property_obj.is_visible is False
    assert blocker in property_obj.publish_blockers
    assert report.properties_pending_review == 1


def test_archived_listing_is_kept_but_hidden() -> None:
    payload = listing_payload()
    payload["specialStatus"] = "archived"

    report = run_full_sync(payload)
    property_obj = Property.objects.get()

    assert property_obj.hostaway_is_active is False
    assert property_obj.is_visible is False
    assert report.properties_deactivated == 0


def test_manual_visibility_override_survives_sync() -> None:
    run_full_sync(listing_payload())
    property_obj = Property.objects.get()
    property_obj.is_visible = False
    property_obj.save()
    assert property_obj.visibility_management == Property.VisibilityManagement.MANUAL

    run_full_sync(listing_payload())
    property_obj.refresh_from_db()

    assert property_obj.is_visible is False
    assert property_obj.visibility_management == Property.VisibilityManagement.MANUAL


def test_missing_listing_requires_two_complete_successful_runs() -> None:
    run_full_sync(listing_payload())
    property_obj = Property.objects.get()

    run_full_sync()
    property_obj.refresh_from_db()
    assert property_obj.consecutive_missing_syncs == 1
    assert property_obj.source_missing is False

    report = run_full_sync()
    property_obj.refresh_from_db()
    assert property_obj.source_missing is True
    assert property_obj.is_visible is False
    assert report.source_missing_marked == 1


def test_incomplete_pagination_does_not_mark_source_missing() -> None:
    property_obj = Property.objects.create(
        hostaway_listing_id=72001,
        slug="existing",
        hostaway_name="Existing",
    )
    client = FakeListingsClient([([], 1)])

    report = sync_properties(client=client, include_amenities=False)
    property_obj.refresh_from_db()

    assert property_obj.consecutive_missing_syncs == 0
    assert property_obj.source_missing is False
    assert report.errors


def test_success_invalidates_public_property_cache() -> None:
    cache.set("properties:list:version", "old")
    cache.set("properties:detail:version", "old")

    run_full_sync(listing_payload())

    assert cache.get("properties:list:version") is None
    assert cache.get("properties:detail:version") is None


def test_failure_does_not_invalidate_cache() -> None:
    cache.set("properties:list:version", "old")
    client = FakeListingsClient([([{"id": None}], 1)])

    sync_properties(client=client, include_amenities=False)

    assert cache.get("properties:list:version") == "old"


def test_publish_readiness_is_read_only() -> None:
    property_obj = Property.objects.create(
        hostaway_listing_id=73001,
        slug="readiness",
        hostaway_name="Ready?",
        person_capacity=2,
        currency_code="SAR",
        is_visible=False,
    )

    result = evaluate_listing_publish_readiness(property_obj)

    assert result.is_ready is False
    assert "missing_visible_image" in result.blockers
    property_obj.refresh_from_db()
    assert property_obj.is_visible is False


def test_verified_map_id_command_is_idempotent_and_refuses_conflict() -> None:
    property_obj = Property.objects.create(
        hostaway_listing_id=315816,
        slug="e12",
        hostaway_name="E12",
    )
    call_command(
        "set_verified_hostaway_identifiers",
        listing_id=315816,
        listing_map_id=315816,
    )
    property_obj.refresh_from_db()
    assert property_obj.hostaway_listing_map_id == 315816
    assert property_obj.hostaway_listing_map_id_verified_at is not None

    with pytest.raises(CommandError, match="differs"):
        call_command(
            "set_verified_hostaway_identifiers",
            listing_id=315816,
            listing_map_id=999999,
        )


def test_distributed_lock_prevents_overlap() -> None:
    cache.clear()
    with distributed_task_lock("test") as first:
        with distributed_task_lock("test") as second:
            assert first is True
            assert second is False


def test_property_task_delegates_to_service() -> None:
    report = Mock(fetched=2, properties_created=1, properties_updated=1, properties_failed=0)
    with patch("apps.integrations.tasks.sync_properties", return_value=report) as service:
        result = sync_hostaway_properties_task.run(dry_run=True)

    service.assert_called_once_with(listing_id=None, dry_run=True)
    assert result["fetched"] == 2


def test_review_task_delegates_to_service() -> None:
    report = Mock(fetched=3, created=2, updated=1, failed=0)
    with patch("apps.integrations.tasks.sync_reviews", return_value=report) as service:
        result = sync_hostaway_reviews_task.run(dry_run=True)

    service.assert_called_once_with(listing_id=None, dry_run=True)
    assert result["created"] == 2


def test_expiration_task_never_creates_reservation_or_payment() -> None:
    expire_booking_objects_task.run()
    assert Reservation.objects.count() == 0
    assert PaymentAttempt.objects.count() == 0


@override_settings(HOSTAWAY_LIVE_BOOKING_ENABLED=True)
def test_paid_booking_recovery_backfills_identifiers_before_scanning() -> None:
    manager = Mock()
    manager.__enter__ = Mock(return_value=Mock())
    manager.__exit__ = Mock(return_value=None)
    with (
        patch("apps.integrations.tasks.call_command") as command,
        patch(
            "apps.reservations.services.hostaway_booking.HostawayBookingService",
            return_value=manager,
        ),
    ):
        result = reconcile_paid_hostaway_reservations_task.run()

    command.assert_called_once_with(
        "backfill_hostaway_listing_map_ids",
        sample=100,
        stdout=command.call_args.kwargs["stdout"],
        stderr=command.call_args.kwargs["stderr"],
    )
    assert result == {
        "status": "completed",
        "backfill": "completed",
        "processed": 0,
        "confirmed": 0,
        "deferred": 0,
        "failed": 0,
    }


def test_beat_schedule_is_disabled_by_default(settings: object) -> None:
    assert settings.HOSTAWAY_AUTO_SYNC_ENABLED is False
    assert settings.CELERY_BEAT_SCHEDULE == {}


def test_sync_run_succeeds_and_is_sanitized() -> None:
    run_full_sync(listing_payload())
    sync_run = IntegrationSyncRun.objects.get()

    assert sync_run.status == IntegrationSyncRun.Status.SUCCEEDED
    assert "authorization" not in str(sync_run.metadata).casefold()
    assert "token" not in str(sync_run.metadata).casefold()


def test_sync_run_is_partially_succeeded_for_one_bad_listing() -> None:
    report = run_full_sync({"id": None}, listing_payload())
    sync_run = IntegrationSyncRun.objects.get()

    assert report.properties_failed == 1
    assert sync_run.status == IntegrationSyncRun.Status.PARTIALLY_SUCCEEDED


def test_admin_health_requires_superuser() -> None:
    user = get_user_model().objects.create_user(username="staff", password="safe-test-pass")
    user.is_staff = True
    user.save()
    client = Client()
    client.force_login(user)

    response = client.get(reverse("admin:integrations_integration_health"))

    assert response.status_code == 403


def test_admin_health_post_requires_csrf() -> None:
    user = get_user_model().objects.create_superuser(
        username="root",
        password="safe-test-pass",
        email="root@example.invalid",
    )
    client = Client(enforce_csrf_checks=True)
    client.force_login(user)

    response = client.post(
        reverse("admin:integrations_integration_health"),
        {"sync_action": "properties_dry_run"},
    )

    assert response.status_code == 403


@override_settings(CELERY_SYNC_DISPATCH_ENABLED=False)
def test_admin_health_does_not_run_long_sync_without_worker() -> None:
    user = get_user_model().objects.create_superuser(
        username="root2",
        password="safe-test-pass",
        email="root2@example.invalid",
    )
    client = Client()
    client.force_login(user)
    with patch("apps.integrations.tasks.sync_hostaway_properties_task.delay") as delay:
        response = client.post(
            reverse("admin:integrations_integration_health"),
            {"sync_action": "properties"},
            follow=True,
        )

    assert response.status_code == 200
    delay.assert_not_called()
    assert Reservation.objects.count() == 0


def test_public_detail_never_renders_private_source_address() -> None:
    property_obj = Property.objects.create(
        hostaway_listing_id=74001,
        slug="private-address",
        hostaway_name="Safe listing",
        city="Riyadh",
        address="Private building and door details",
        public_address="Private building and door details",
        is_visible=True,
    )

    response = Client().get(property_obj.get_absolute_url())

    assert response.status_code == 200
    assert b"Private building and door details" not in response.content
    assert b"Riyadh" in response.content
