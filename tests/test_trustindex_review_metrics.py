from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest
from django.core.management import call_command

from apps.properties.models import Property
from apps.reviews.trustindex import fetch_widget_metrics

pytestmark = pytest.mark.django_db


def test_fetch_widget_metrics_normalises_booking_scores_to_five() -> None:
    response = MagicMock()
    response.read.return_value = (
        b'<span class="ti-header-rating">9.7</span>'
        b'<span class="ti-header-rating-reviews">28 reviews</span>'
        b'<div data-max-rating="10">'
    )
    context_manager = MagicMock()
    context_manager.__enter__.return_value = response
    context_manager.__exit__.return_value = None

    with patch("apps.reviews.trustindex.urlopen", return_value=context_manager):
        metrics = fetch_widget_metrics("a" * 24)

    assert metrics.rating == Decimal("4.9")
    assert metrics.review_count == 28


def test_metric_command_updates_only_properties_with_an_approved_widget() -> None:
    property_obj = Property.objects.create(
        hostaway_listing_id=9901,
        slug="trustindex-test",
        name_ar="وحدة تجريبية",
        country_code="SA",
        trustindex_widget_id="b" * 24,
    )
    Property.objects.create(
        hostaway_listing_id=9902,
        slug="without-trustindex",
        name_ar="وحدة بلا مراجعات",
        country_code="SA",
    )

    with patch(
        "apps.reviews.management.commands.sync_trustindex_review_metrics.fetch_widget_metrics",
        return_value=type("Metrics", (), {"rating": Decimal("4.6"), "review_count": 12})(),
    ):
        call_command("sync_trustindex_review_metrics")

    property_obj.refresh_from_db()
    assert property_obj.trustindex_rating == Decimal("4.6")
    assert property_obj.trustindex_review_count == 12
