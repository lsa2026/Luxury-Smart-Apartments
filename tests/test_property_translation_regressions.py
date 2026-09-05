"""Guest-facing translations that must not silently fall back to English."""

import pytest
from django.utils.translation import gettext, ngettext, override


@pytest.mark.parametrize(
    ("language", "expected"),
    [
        (
            "ar",
            {
                "summary": "4.9 عبر جميع قنوات الحجز",
                "reviews": "مراجعتان",
                "price": "ابتداءً من SAR 700.00 لليلة",
                "policy": "قبل الحجز",
            },
        ),
        (
            "fr",
            {
                "summary": "4.9 sur l’ensemble des canaux de réservation",
                "reviews": "2 avis",
                "price": "À partir de SAR 700.00 / nuit",
                "policy": "Avant de réserver",
            },
        ),
    ],
)
def test_property_page_copy_is_translated_in_every_supported_guest_language(
    language: str,
    expected: dict[str, str],
) -> None:
    with override(language):
        summary = gettext("%(rating)s across all booking channels") % {"rating": "4.9"}
        review_count = ngettext("%(counter)s review", "%(counter)s reviews", 2) % {"counter": 2}
        price = gettext("From %(amount)s / night") % {"amount": "SAR 700.00"}

        assert summary == expected["summary"]
        assert review_count == expected["reviews"]
        assert price == expected["price"]
        assert gettext("Before you book") == expected["policy"]
