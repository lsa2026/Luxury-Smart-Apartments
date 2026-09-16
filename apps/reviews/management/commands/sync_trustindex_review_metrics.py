"""Refresh display metrics from approved public Trustindex widgets."""

from django.core.management.base import BaseCommand
from django.utils import timezone

from apps.properties.models import Property
from apps.properties.trustindex import metrics_widget_id
from apps.reviews.trustindex import TrustindexFetchError, fetch_widget_metrics


class Command(BaseCommand):
    help = "Refresh local rating and review-count metrics from Trustindex widgets."

    def handle(self, *args: object, **options: object) -> None:
        refreshed = failed = 0
        for property_obj in Property.objects.exclude(trustindex_widget_id="").order_by("id"):
            try:
                metrics = fetch_widget_metrics(metrics_widget_id(property_obj))
            except TrustindexFetchError as exc:
                failed += 1
                self.stderr.write(self.style.WARNING(f"{property_obj.hostaway_listing_id}: {exc}"))
                continue
            property_obj.trustindex_rating = metrics.rating
            property_obj.trustindex_review_count = metrics.review_count
            property_obj.trustindex_synced_at = timezone.now()
            property_obj.save(
                update_fields=[
                    "trustindex_rating",
                    "trustindex_review_count",
                    "trustindex_synced_at",
                ]
            )
            refreshed += 1
        self.stdout.write(
            self.style.SUCCESS(f"Refreshed {refreshed} Trustindex widgets; {failed} failed.")
        )
