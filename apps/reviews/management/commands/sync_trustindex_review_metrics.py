"""Refresh local display metrics from the public Trustindex property widgets."""

from django.core.management.base import BaseCommand
from django.utils import timezone

from apps.properties.models import Property
from apps.properties.trustindex import metrics_widget_id
from apps.reviews.trustindex import TrustindexFetchError, fetch_widget_metrics


class Command(BaseCommand):
    help = "Refresh local rating and review-count metrics from Trustindex widgets."

    def add_arguments(self, parser: object) -> None:
        parser.add_argument("--dry-run", action="store_true")

    def handle(self, *args: object, **options: object) -> None:
        dry_run = bool(options["dry_run"])
        refreshed = failed = 0
        for property_obj in Property.objects.exclude(trustindex_widget_id="").order_by("id"):
            widget_id = metrics_widget_id(property_obj)
            try:
                metrics = fetch_widget_metrics(widget_id)
            except TrustindexFetchError as exc:
                failed += 1
                self.stderr.write(self.style.WARNING(f"{property_obj.hostaway_listing_id}: {exc}"))
                continue
            refreshed += 1
            if not dry_run:
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
            self.stdout.write(
                f"{property_obj.hostaway_listing_id}: {metrics.rating}/5, "
                f"{metrics.review_count} reviews"
            )
        self.stdout.write(
            self.style.SUCCESS(
                f"{'Would refresh' if dry_run else 'Refreshed'} {refreshed} Trustindex widgets; "
                f"{failed} failed."
            )
        )
