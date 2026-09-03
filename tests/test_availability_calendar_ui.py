"""Markup contract for the stay-dates calendar.

The calendar itself is driven by JavaScript, so what is asserted here is the
wiring the script depends on: the hooks it queries for and the state each
control starts in.
"""

import re

import pytest
from django.test import Client

from apps.properties.models import Property

pytestmark = pytest.mark.django_db


def calendar_dialog(content: str) -> str:
    start = content.index("data-luxury-calendar")
    return content[start : content.index("</dialog>", start)]


def test_calendar_offers_a_way_to_clear_the_chosen_dates() -> None:
    dialog = calendar_dialog(Client().get("/").content.decode())

    assert "data-calendar-clear" in dialog


def test_clear_starts_disabled_so_it_cannot_be_pressed_before_a_date_is_chosen() -> None:
    dialog = calendar_dialog(Client().get("/").content.decode())

    clear_button = re.search(r"<button[^>]*data-calendar-clear[^>]*>", dialog)
    assert clear_button is not None
    assert "disabled" in clear_button.group(0)


def test_clear_sits_beside_confirm_rather_than_replacing_it() -> None:
    dialog = calendar_dialog(Client().get("/").content.decode())

    # Both controls belong to the footer: clearing must not remove the way to
    # accept the dates, and confirming must not remove the way to start again.
    assert "data-calendar-confirm" in dialog
    assert dialog.index("data-calendar-clear") < dialog.index("data-calendar-confirm")


def test_clear_is_reachable_on_a_property_page_too() -> None:
    # The calendar ships as one include, so a guest must never meet a copy of
    # it that cannot be reset.
    property_obj = Property.objects.create(
        hostaway_listing_id=9001,
        slug="calendar-property",
        hostaway_name="Source 9001",
        name_ar="وحدة التقويم",
        name_en="Calendar property",
        city="Riyadh",
        city_ar="الرياض",
        country_code="SA",
        currency_code="SAR",
        is_visible=True,
    )

    dialog = calendar_dialog(
        Client().get(property_obj.get_absolute_url()).content.decode()
    )

    assert "data-calendar-clear" in dialog
