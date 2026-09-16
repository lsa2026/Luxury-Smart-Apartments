"""Public review summaries sourced only from Trustindex."""

from dataclasses import dataclass
from decimal import Decimal

from django.db.models import QuerySet


@dataclass(frozen=True)
class RatingSummary:
    average_out_of_five: Decimal | None
    review_count: int

    @property
    def has_reviews(self) -> bool:
        return self.average_out_of_five is not None and self.review_count > 0

    # Backward-compatible names keep the established SEO markup aligned with
    # the Trustindex figure without retaining any Hostaway review source.
    @property
    def has_published(self) -> bool:
        return self.has_reviews

    @property
    def published_average_out_of_five(self) -> Decimal | None:
        return self.average_out_of_five

    @property
    def published_count(self) -> int:
        return self.review_count

    @property
    def all_channel_average_out_of_five(self) -> Decimal | None:
        return None

    @property
    def differs_from_all_channels(self) -> bool:
        return False


def with_published_rating(queryset: QuerySet) -> QuerySet:
    """Compatibility helper; public ratings now live directly on properties."""
    return queryset


def rating_summary(property_obj: object) -> RatingSummary:
    widget_id = getattr(property_obj, "trustindex_widget_id", "")
    rating = getattr(property_obj, "trustindex_rating", None)
    review_count = getattr(property_obj, "trustindex_review_count", 0) or 0
    if not widget_id or rating is None or review_count <= 0:
        return RatingSummary(average_out_of_five=None, review_count=0)
    return RatingSummary(average_out_of_five=rating, review_count=review_count)
