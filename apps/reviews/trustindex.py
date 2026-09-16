"""Read the aggregate visibly published by a Trustindex widget."""

import re
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from django.conf import settings

_WIDGET_ID = re.compile(r"^[A-Za-z0-9]{20,32}$")
_RATING = re.compile(r'<span class="ti-header-rating">\s*([0-9]{1,2}(?:\.\d)?)\s*</span>')
_COUNT = re.compile(r'<span class="ti-header-rating-reviews">\s*([\d,]+)\s+reviews?\s*</span>')
_MAX_RATING = re.compile(r'data-max-rating="([1-9][0-9]?(?:\.\d+)?)"')


class TrustindexFetchError(RuntimeError):
    """The public review aggregate could not be safely read."""


@dataclass(frozen=True)
class TrustindexMetrics:
    rating: Decimal
    review_count: int


def fetch_widget_metrics(widget_id: str, *, timeout: float = 15.0) -> TrustindexMetrics:
    if not _WIDGET_ID.fullmatch(widget_id):
        raise TrustindexFetchError("The Trustindex widget identifier is invalid.")
    url = f"https://cdn.trustindex.io/widgets/{widget_id[:2].lower()}/{widget_id}/content.html"
    request = Request(
        url,
        headers={
            "User-Agent": "LuxurySmartApartments/1.0 review-metric-refresh",
            "Referer": settings.SITE_CANONICAL_URL.rstrip("/") + "/",
        },
    )
    try:
        with urlopen(request, timeout=timeout) as response:  # noqa: S310 - fixed provider domain
            content = response.read().decode("utf-8", errors="replace")
    except (HTTPError, URLError, OSError) as exc:
        raise TrustindexFetchError("Trustindex widget could not be retrieved.") from exc
    rating_match = _RATING.search(content)
    count_match = _COUNT.search(content)
    if not rating_match or not count_match:
        raise TrustindexFetchError("Trustindex widget did not contain a visible rating summary.")
    try:
        rating = Decimal(rating_match.group(1))
        count = int(count_match.group(1).replace(",", ""))
    except (InvalidOperation, ValueError) as exc:
        raise TrustindexFetchError("Trustindex rating summary was invalid.") from exc
    max_rating_match = _MAX_RATING.search(content)
    if rating > Decimal("5") and max_rating_match:
        rating = (rating * Decimal("5") / Decimal(max_rating_match.group(1))).quantize(
            Decimal("0.1"), rounding=ROUND_HALF_UP
        )
    if not Decimal("0") <= rating <= Decimal("5") or count <= 0:
        raise TrustindexFetchError("Trustindex rating summary was outside its valid range.")
    return TrustindexMetrics(rating=rating, review_count=count)
