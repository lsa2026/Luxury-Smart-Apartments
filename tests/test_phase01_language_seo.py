from decimal import Decimal

import pytest
from django.conf import settings
from django.core.cache import cache
from django.test import Client
from django.utils import translation

from apps.core.seo import property_structured_data
from apps.properties.models import Property, PropertyImage

pytestmark = pytest.mark.django_db


@pytest.fixture
def property_obj() -> Property:
    return Property.objects.create(
        hostaway_listing_id=880001,
        slug="phase-one-stay",
        hostaway_name="Phase One Stay",
        name_ar="إقامة المرحلة الأولى",
        name_en="Phase One Stay",
        name_fr="Séjour phase un",
        description_ar="وصف عربي للاختبار.",
        description_en="English test description.",
        description_fr="Description française de test.",
        city="Riyadh",
        country_code="SA",
        person_capacity=4,
        bedrooms_number=2,
        bathrooms_number=Decimal("2.0"),
        is_visible=True,
    )


def test_old_public_urls_redirect_permanently_to_arabic(property_obj: Property) -> None:
    client = Client()
    assert client.get("/").headers["Location"] == "/ar/"
    assert client.get("/about").headers["Location"] == "/ar/about/"
    response = client.get("/properties/phase-one-stay/?source=old")
    assert response.status_code == 301
    assert response.headers["Location"] == "/ar/properties/phase-one-stay/?source=old"


@pytest.mark.parametrize("language", ["ar", "en", "fr"])
def test_each_public_language_has_a_real_url(language: str) -> None:
    response = Client().get(f"/{language}/")
    assert response.status_code == 200
    assert f'lang="{language}"' in response.content.decode()


def test_private_and_technical_urls_remain_unprefixed() -> None:
    client = Client()
    assert client.get("/login/").status_code == 200
    assert client.get("/robots.txt").status_code == 200
    assert client.get("/sitemap.xml").status_code == 200


def test_canonical_and_hreflang_are_distinct_and_reciprocal(
    property_obj: Property,
) -> None:
    base_url = settings.SITE_CANONICAL_URL
    for language in ("ar", "en", "fr"):
        content = Client().get(f"/{language}/properties/{property_obj.slug}/").content.decode()
        canonical = f'{base_url}/{language}/properties/{property_obj.slug}/'
        assert f'rel="canonical" href="{canonical}"' in content
        for alternate in ("ar", "en", "fr"):
            assert (
                f'hreflang="{alternate}" '
                f'href="{base_url}/{alternate}/properties/{property_obj.slug}/"'
            ) in content
        assert (
            f'hreflang="x-default" '
            f'href="{base_url}/ar/properties/{property_obj.slug}/"'
        ) in content


def test_sitemap_lists_each_language_as_a_location_with_the_same_alternates(
    property_obj: Property,
) -> None:
    cache.clear()
    content = Client().get("/sitemap.xml").content.decode()
    base_url = settings.SITE_CANONICAL_URL
    for language in ("ar", "en", "fr"):
        url = f"{base_url}/{language}/properties/{property_obj.slug}/"
        assert f"<loc>{url}</loc>" in content
        assert f'hreflang="{language}" href="{url}"' in content
    assert f'hreflang="x-default" href="{base_url}/ar/properties/{property_obj.slug}/"' in content


def test_robots_does_not_block_every_query_string() -> None:
    content = Client().get("/robots.txt").content.decode()
    assert "Disallow: /*?" not in content
    assert "Disallow: /admin/" in content


@pytest.mark.parametrize(
    ("source", "destination"),
    [
        ("/about-us/", "/ar/about/"),
        ("/contact-us/", "/ar/contact/"),
        (
            "/listing/darat-safa-luxury-apartment/",
            "/ar/properties/darat-safa-luxury-apartment/",
        ),
        (
            "/listing/luxury-smart-apartment-a11/",
            "/ar/properties/luxury-smart-apartment-a11/",
        ),
        (
            "/listing/luxury-smart-apartment-e12/",
            "/ar/properties/luxury-smart-apartment-e12/",
        ),
        (
            "/listing/luxury-smart-apartment-b12/",
            "/ar/properties/luxury-smart-apartment-b12/",
        ),
        ("/listing/al-hamra-family-villa/", "/ar/properties/al-hamra-family-villa/"),
        (
            "/listing/luxury-smart-apartment-at-nour-prestige-marrakech/",
            "/ar/properties/luxury-smart-apartment-at-nour-prestige-marrakech/",
        ),
    ],
)
def test_confirmed_legacy_urls_have_exact_permanent_redirects(
    source: str,
    destination: str,
) -> None:
    response = Client().get(source)
    assert response.status_code == 301
    assert response.headers["Location"] == destination


def test_incomplete_vacation_rental_uses_safe_general_schema(property_obj: Property) -> None:
    data = property_structured_data(property_obj)
    assert data["@type"] == "LodgingBusiness"
    assert "containsPlace" not in data


def test_complete_vacation_rental_schema_meets_required_local_fields(
    property_obj: Property,
) -> None:
    property_obj.public_location_enabled = True
    property_obj.public_location_latitude = Decimal("24.713552")
    property_obj.public_location_longitude = Decimal("46.675296")
    property_obj.save()
    for index in range(8):
        PropertyImage.objects.create(
            property=property_obj,
            hostaway_image_id=880100 + index,
            hostaway_url=f"https://example.invalid/phase-one-{index}.jpg",
            sync_key=f"id:{880100 + index}",
            source=PropertyImage.Source.HOSTAWAY,
            is_visible=True,
            is_cover=index == 0,
            alt_text_ar=f"صورة اختبار {index + 1}",
            alt_text_en=f"Test image {index + 1}",
        )

    with translation.override("en"):
        data = property_structured_data(property_obj)

    assert data["@type"] == "VacationRental"
    assert len(data["image"]) == 8
    assert data["containsPlace"]["@type"] == "Accommodation"
    assert data["containsPlace"]["occupancy"]["value"] == 4
    assert data["containsPlace"]["numberOfBedrooms"] == 2
    assert data["geo"]["latitude"] == 24.713552
    assert data["url"].endswith("/en/properties/phase-one-stay/")
