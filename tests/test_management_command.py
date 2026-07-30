from io import StringIO
from unittest.mock import patch

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError

from apps.integrations.hostaway.property_services import PropertySyncReport
from apps.integrations.hostaway.services import SyncReport


def test_management_command_dry_run_passes_option_and_prints_summary() -> None:
    output = StringIO()
    with patch(
        "apps.reviews.management.commands.sync_hostaway_reviews.sync_reviews",
        return_value=SyncReport(fetched=2, created=1, skipped=1),
    ) as sync_mock:
        call_command("sync_hostaway_reviews", "--dry-run", stdout=output)

    assert sync_mock.call_args.kwargs["dry_run"] is True
    assert "dry run" in output.getvalue()
    assert "fetched=2" in output.getvalue()


def test_management_command_returns_failure_for_fully_failed_sync() -> None:
    with (
        patch(
            "apps.reviews.management.commands.sync_hostaway_reviews.sync_reviews",
            return_value=SyncReport(fetched=1, failed=1),
        ),
        pytest.raises(CommandError, match="failed=1"),
    ):
        call_command("sync_hostaway_reviews")


def test_property_management_command_defaults_and_dry_run() -> None:
    output = StringIO()
    with patch(
        "apps.properties.management.commands.sync_hostaway_properties.sync_properties",
        return_value=PropertySyncReport(fetched=1, properties_created=1),
    ) as sync_mock:
        call_command(
            "sync_hostaway_properties",
            "--dry-run",
            "--listing-id",
            "315814",
            stdout=output,
        )

    assert sync_mock.call_args.kwargs["dry_run"] is True
    assert sync_mock.call_args.kwargs["listing_id"] == 315814
    assert sync_mock.call_args.kwargs["include_images"] is True
    assert sync_mock.call_args.kwargs["include_amenities"] is True
    assert "properties_created=1" in output.getvalue()
