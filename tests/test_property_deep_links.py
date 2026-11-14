"""Public property links retain their language, apartment and read-only intent."""

import os
from pathlib import Path
from unittest.mock import patch

import pytest
from django.test import Client

from apps.properties.models import PropertyImage
from apps.reservations.models import BookingQuote
from tests.test_indicative_rates import make_property

pytestmark = pytest.mark.django_db


@pytest.mark.parametrize("language", ["ar", "en", "fr"])
def test_property_deep_link_pages(language, tmp_path):
    property_obj = make_property()
    make_property(9102)  # Similar-property cards must not duplicate the dialog or win the link.
    PropertyImage.objects.create(property=property_obj, image="synthetic-property.jpg")
    root = f"/{language}/properties/{property_obj.slug}/"
    output = Path(os.environ.get("LSA_CONTACT_VISUAL_DIR", tmp_path))
    output.mkdir(parents=True, exist_ok=True)
    with patch(
        "apps.integrations.hostaway.client.HostawayClient",
        side_effect=AssertionError("Opening a link must not request a live quote"),
    ):
        for name, url in (("detail", root), ("gallery", root + "gallery/")):
            response = Client().get(url)
            assert response.status_code == 200
            html = response.content.decode()
            assert f'<html lang="{language}"' in html
            assert "luxury-living" in html
            if name == "detail":
                assert html.count("data-property-price-calendar ") == 1
                assert f'data-price-calendar-url="{root}price-calendar/"' in html
                assert 'id="price-calendar"' in html
                assert 'href="#availability" data-price-calendar-open' in html
                assert html.count('<dialog class="price-calendar"') == 1
                assert 'id="availability"' in html
                assert 'src="/static/js/price-calendar.js' not in html
                assert 'href="/static/css/price-calendar.css' not in html
            else:
                assert "gallery-page__item" in html
                assert "synthetic-property.jpg" in html
                assert "data-lightbox-open" in html
            (output / f"property-link-{name}-{language}.html").write_text(html)
        listing = Client().get(f"/{language}/properties/").content.decode()
        assert f'href="{root}#price-calendar"' in listing
        (output / f"property-link-list-{language}.html").write_text(listing)
    assert not BookingQuote.objects.exists()
