"""One public review summary, sourced only from Trustindex.

The site does not calculate or display Hostaway reviews. Trustindex renders the
review content and platform attribution; its visible aggregate is copied
locally for cards, sorting and structured data.
"""

from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True)
class RatingSummary:
    average_out_of_five: Decimal | None
    review_count: int

    @property
    def has_reviews(self) -> bool:
        return self.average_out_of_five is not None and self.review_count > 0


def rating_summary(property_obj: object) -> RatingSummary:
    """Return only the aggregate rendered by the property's Trustindex widget."""
    widget_id = getattr(property_obj, "trustindex_widget_id", "")
    rating = getattr(property_obj, "trustindex_rating", None)
    review_count = getattr(property_obj, "trustindex_review_count", 0) or 0
    if not widget_id or rating is None or review_count <= 0:
        return RatingSummary(average_out_of_five=None, review_count=0)
    return RatingSummary(
        average_out_of_five=rating,
        review_count=review_count,
    )
