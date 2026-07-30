from io import StringIO
from unittest.mock import patch

import httpx
import pytest
from django.core.management import call_command
from django.core.management.base import CommandError

from apps.integrations.hostaway.client import HostawayClient
from apps.integrations.hostaway.listing_validators import (
    HostawayCollectionPage,
    HostawayObjectDocument,
)
from apps.integrations.hostaway.verification import (
    ListingVerification,
    VerificationReport,
    verify_hostaway,
)
from apps.integrations.models import IntegrationSyncRun
from apps.properties.models import Property

pytestmark = pytest.mark.django_db


class FakeVerificationClient:
    masked_account_id = "****1234"

    def __init__(self, records: list[dict[str, object]]) -> None:
        self.records = records
        self.authenticated = False

    def authenticate(self) -> None:
        self.authenticated = True

    def get_listings_page(self, **kwargs: object) -> HostawayCollectionPage:
        return HostawayCollectionPage(
            records=tuple(self.records),
            status="success",
            limit=3,
            offset=0,
            count=len(self.records),
            page=1,
            total_pages=1,
            fields=frozenset(
                {"status", "result", "limit", "offset", "count", "page", "totalPages"}
            ),
        )

    def get_listing(
        self,
        listing_id: int,
        *,
        include_resources: bool,
    ) -> dict[str, object]:
        return self.records[0]

    def get_listing_document(
        self,
        listing_id: int,
        *,
        include_resources: bool,
    ) -> HostawayObjectDocument:
        return HostawayObjectDocument(
            record=self.records[0],
            status="success",
            fields=frozenset({"status", "result"}),
        )

    def get_amenities_page(self) -> HostawayCollectionPage:
        return HostawayCollectionPage(
            records=({"id": 1, "name": "Wifi"},),
            status="success",
            limit=None,
            offset=None,
            count=1,
            page=1,
            total_pages=1,
            fields=frozenset({"status", "result", "count"}),
        )

    def get_reviews(self, **kwargs: object) -> tuple[list[dict[str, object]], int]:
        return (
            [
                {
                    "id": 9,
                    "listingMapId": 40160,
                    "rating": 9.5,
                    "publicReview": "Safe public text",
                    "privateFeedback": "must-never-appear",
                    "guestName": "Private Guest",
                    "reservationId": "private-reservation",
                }
            ],
            1,
        )


def test_partial_listing_unknown_fields_and_primary_id_do_not_write_database() -> None:
    client = FakeVerificationClient(
        [
            {
                "id": 40160,
                "name": "Unit",
                "specialStatus": None,
                "futureHostawayField": {"shape": "new"},
            }
        ]
    )
    before = (Property.objects.count(), IntegrationSyncRun.objects.count())

    report = verify_hostaway(client=client)

    assert client.authenticated is True
    assert report.listings[0].identifier == 40160
    assert "listingMapId" in report.listings[0].missing_fields
    assert report.listings[0].unknown_fields == ["futureHostawayField"]
    assert report.listings[0].field_types["id"] == "integer"
    assert not hasattr(report, "raw_response")
    assert (Property.objects.count(), IntegrationSyncRun.objects.count()) == before


def test_incompatible_basic_types_are_reported_for_strict_mode() -> None:
    client = FakeVerificationClient([{"id": {"invalid": True}, "name": ["invalid"], "newField": 1}])

    report = verify_hostaway(client=client)

    assert {"id", "name"}.issubset(report.listings[0].type_mismatches)
    assert report.strict_errors
    assert report.listings[0].unknown_fields == ["newField"]


def test_single_listing_reports_actual_detail_envelope() -> None:
    client = FakeVerificationClient([{"id": 40160, "name": "Unit"}])

    report = verify_hostaway(client=client, listing_id=40160)

    assert report.root_present_fields == ["result", "status"]
    assert "totalPages" in report.root_missing_fields
    assert report.root_field_types["result"] == "object"


def test_review_schema_excludes_private_and_guest_identifiers() -> None:
    client = FakeVerificationClient([{"id": 40160, "name": "Unit"}])

    report = verify_hostaway(client=client, include_reviews=True)

    assert report.reviews is not None
    assert report.reviews.matched_listing_ids == 1
    assert report.reviews.unmatched_listing_ids == 0
    assert "privateFeedback" not in report.reviews.field_types
    assert "guestName" not in report.reviews.field_types
    assert "reservationId" not in report.reviews.field_types


def test_verification_uses_only_read_endpoints() -> None:
    methods: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        methods.append(request.method)
        if request.url.path.endswith("/listings"):
            payload = {
                "status": "success",
                "result": [{"id": 40160, "name": "Unit"}],
                "limit": 3,
                "offset": 0,
                "count": 1,
                "page": 1,
                "totalPages": 1,
            }
        elif request.url.path.endswith("/amenities"):
            payload = {"status": "success", "result": [{"id": 1, "name": "Wifi"}]}
        else:
            payload = {"status": "success", "result": [], "count": 0}
        return httpx.Response(200, json=payload, request=request)

    with httpx.Client(
        transport=httpx.MockTransport(handler),
        base_url="https://api.hostaway.com/v1",
    ) as http:
        client = HostawayClient(access_token="test-token", client=http)
        report = verify_hostaway(
            client=client,
            include_reviews=True,
            include_amenities=True,
        )

    assert report.connection_ok is True
    assert methods == ["GET", "GET", "GET"]


def test_management_command_schema_contains_types_not_sensitive_values() -> None:
    output = StringIO()
    report = VerificationReport(
        connection_ok=True,
        authentication_seconds=0.01,
        masked_account_id="****1234",
        root_present_fields=["status", "result"],
        root_missing_fields=[],
        root_unknown_fields=[],
        root_field_types={"status": "string", "result": "array"},
        listings=[
            ListingVerification(
                identifier=40160,
                present_fields=["id", "name"],
                missing_fields=[],
                unknown_fields=[],
                field_types={"id": "integer", "name": "string"},
                type_mismatches=[],
                validation_errors=[],
                special_status="",
            )
        ],
        recommendations=["No blocking changes."],
    )

    with patch(
        "apps.integrations.management.commands.verify_hostaway_integration.verify_hostaway",
        return_value=report,
    ) as verify_mock:
        call_command(
            "verify_hostaway_integration",
            "--show-schema",
            stdout=output,
        )

    rendered = output.getvalue()
    assert "id:integer" in rendered
    assert "name:string" in rendered
    assert "access_token" not in rendered
    assert "Authorization" not in rendered
    assert "privateFeedback" not in rendered
    assert verify_mock.call_args.kwargs["listing_limit"] == 3
    assert verify_mock.call_args.kwargs["include_reviews"] is False
    assert verify_mock.call_args.kwargs["include_amenities"] is False


def test_management_command_strict_returns_nonzero_for_basic_mismatch() -> None:
    report = VerificationReport(
        connection_ok=True,
        authentication_seconds=0.01,
        masked_account_id="****1234",
        root_present_fields=["status", "result"],
        root_missing_fields=[],
        root_unknown_fields=[],
        root_field_types={},
        listings=[
            ListingVerification(
                identifier=None,
                present_fields=["id"],
                missing_fields=["name"],
                unknown_fields=[],
                field_types={"id": "object"},
                type_mismatches=["id"],
                validation_errors=["invalid id"],
                special_status="",
            )
        ],
    )

    with (
        patch(
            "apps.integrations.management.commands.verify_hostaway_integration.verify_hostaway",
            return_value=report,
        ),
        pytest.raises(CommandError, match="Strict verification failed"),
    ):
        call_command("verify_hostaway_integration", "--strict")
