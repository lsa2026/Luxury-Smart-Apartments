"""One rating figure, used by the page and by its structured data alike.

Two numbers existed for the same property. The page showed
``Property.average_review_rating``, which Hostaway computes across every channel
it manages, while the JSON-LD averaged only the reviews published on this site.
A visitor could read 4.9 above a list of reviews averaging 4.1.

The published reviews win, for two reasons: they are what the visitor can
actually count on the page, and search engines require ``aggregateRating`` to
describe ratings visible on that page. The all-channel figure is not discarded —
it is returned separately so a template can show it under its own label, where
it reads as extra information rather than as a contradiction.
"""

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from typing import Final

from django.db.models import Avg, Count

from apps.reviews.models import Review

# Hostaway rates out of ten; the site presents five stars.
_SCALE: Final = Decimal("2")


@dataclass(frozen=True)
class RatingSummary:
    published_average_out_of_five: Decimal | None
    published_count: int
    all_channel_average_out_of_five: Decimal | None

    @property
    def has_published(self) -> bool:
        return self.published_average_out_of_five is not None and self.published_count > 0

    @property
    def differs_from_all_channels(self) -> bool:
        """True when the wider average is worth showing beside the page's own."""
        if not self.has_published or self.all_channel_average_out_of_five is None:
            return False
        return self.all_channel_average_out_of_five != self.published_average_out_of_five


def _out_of_five(value: object) -> Decimal | None:
    if value in (None, ""):
        return None
    try:
        return (Decimal(str(value)) / _SCALE).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP)
    except (ArithmeticError, TypeError, ValueError):
        return None


def rating_summary(property_obj: object) -> RatingSummary:
    """The figures for one property, both derived the same way every time."""
    aggregate = (
        Review.objects.public()
        .filter(property=property_obj)
        .aggregate(average=Avg("rating"), count=Count("id"))
    )
    return RatingSummary(
        published_average_out_of_five=_out_of_five(aggregate["average"]),
        published_count=aggregate["count"] or 0,
        all_channel_average_out_of_five=_out_of_five(
            getattr(property_obj, "average_review_rating", None)
        ),
    )
