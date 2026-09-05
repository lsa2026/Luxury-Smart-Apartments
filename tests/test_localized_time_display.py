"""Timestamps shown to a guest must read in Riyadh time, not UTC.

Django converts an aware datetime when a template prints it directly, but a
custom filter receives the raw value and ``formats.date_format`` performs no
conversion. Every timestamp on the site goes through these filters, so the
conversion has to happen inside them.
"""

import datetime as dt
from pathlib import Path

import pytest
from django.conf import settings
from django.test import Client
from django.utils import timezone, translation

from apps.core.templatetags.presentation import (
    localized_date,
    localized_datetime,
    localized_month_year,
)

# 03 Sep 2026 23:46 UTC is 04 Sep 2026 02:46 in Riyadh: the case from the audit,
# where UTC rendering also dragged the displayed date back to the previous day.
UTC_INSTANT = dt.datetime(2026, 9, 3, 23, 46, tzinfo=dt.UTC)


def test_the_site_runs_on_riyadh_time() -> None:
    assert settings.TIME_ZONE == "Asia/Riyadh"
    assert settings.USE_TZ is True


def test_a_stored_utc_instant_is_shown_in_riyadh_time() -> None:
    with translation.override("en"):
        rendered = localized_datetime(UTC_INSTANT)

    assert rendered == "4 September 2026 02:46"


def test_the_displayed_day_is_the_local_day_not_the_utc_one() -> None:
    with translation.override("en"):
        assert localized_date(UTC_INSTANT) == "4 September 2026"


def test_arabic_rendering_localises_the_same_instant() -> None:
    with translation.override("ar"):
        rendered = localized_datetime(UTC_INSTANT)

    # Arabic-Indic digits for 04 … 2026 … 02:46.
    assert "٤" in rendered
    assert "٠٢:٤٦" in rendered


def test_month_and_year_follow_the_local_calendar() -> None:
    # 31 Dec 2026 22:00 UTC is 01 Jan 2027 01:00 in Riyadh: the year rolls over.
    new_year_eve = dt.datetime(2026, 12, 31, 22, 0, tzinfo=dt.UTC)

    with translation.override("en"):
        assert localized_month_year(new_year_eve) == "January 2027"


def test_a_plain_date_is_left_untouched() -> None:
    """A date has no time to convert; shifting it would move the stay."""
    with translation.override("en"):
        assert localized_date(dt.date(2026, 9, 4)) == "4 September 2026"


def test_a_naive_datetime_does_not_raise() -> None:
    """``localtime`` refuses naive values, so they must fall through."""
    naive = dt.datetime(2026, 9, 4, 2, 46)

    with translation.override("en"):
        assert localized_datetime(naive) == "4 September 2026 02:46"


@pytest.mark.parametrize("value", [None, "", "not-a-date"])
def test_unusable_values_render_as_empty(value: object) -> None:
    assert localized_datetime(value) == ""


@pytest.mark.django_db
def test_a_quote_page_shows_its_expiry_in_riyadh_time() -> None:
    """The end-to-end case: the expiry a guest reads on the page."""
    from tests.test_account_booking_claim import make_reservation

    reservation = make_reservation("LSA-TZ-1")
    quote = reservation.booking_intent.quote
    quote.expires_at = UTC_INSTANT
    quote.save(update_fields=["expires_at"])

    with translation.override("en"):
        rendered = localized_datetime(quote.expires_at)

    local = timezone.localtime(quote.expires_at)
    assert local.day == 4
    assert rendered == "4 September 2026 02:46"


@pytest.mark.django_db
def test_client_pages_still_render(client: Client) -> None:
    # Guards against an import-time mistake in the filter module.
    assert client.get("/").status_code == 200


# --- hero imagery must not depend on an animation ---------------------------


def _stylesheet() -> str:
    return (Path(settings.BASE_DIR) / "static" / "css" / "site.css").read_text(encoding="utf-8")


@pytest.mark.parametrize(
    "animation",
    ["hero-image-arrive", "hero-copy-in", "skyline-rise", "travel-hero-image"],
)
def test_content_entrance_animations_do_not_hide_their_element_at_rest(
    animation: str,
) -> None:
    """``both`` holds the first keyframe, so content stays at opacity 0 unless
    the animation actually runs. The resting state must be the visible one."""
    css = _stylesheet()

    declaration = next(line for line in css.splitlines() if f"animation: {animation} " in line)
    assert "both" not in declaration, declaration
    assert "forwards" in declaration, declaration


@pytest.mark.django_db
def test_the_home_page_marks_its_third_party_images_for_fallback(client: Client) -> None:
    content = client.get("/").content.decode()

    # The Marrakech postcard and both destination cards.
    assert content.count("data-image-fallback") >= 2
