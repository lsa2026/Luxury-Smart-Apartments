"""Phase 10 tests use synthetic data and never contact Google or Hostaway."""

from datetime import date
from decimal import Decimal
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.test import Client, override_settings
from django.urls import reverse
from django.utils import timezone

from apps.core.checks import validate_google_configuration
from apps.core.marketing import (
    EVENT_SCHEMAS,
    prepare_purchase_event,
    property_analytics_item,
    sanitize_analytics_event,
)
from apps.core.models import LegacyRedirect, MarketingEventReceipt
from apps.core.seo import property_structured_data
from apps.payments.models import PaymentAttempt
from apps.properties.models import Property, PropertyImage
from apps.reservations.models import Reservation
from apps.reviews.models import Review

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def clear_phase10_cache() -> None:
    cache.clear()


@pytest.fixture
def property_obj() -> Property:
    return Property.objects.create(
        hostaway_listing_id=990001,
        slug="synthetic-smart-stay",
        hostaway_name="Synthetic Smart Stay",
        hostaway_description="Synthetic public description.",
        name_ar="إقامة ذكية اصطناعية",
        name_en="Synthetic Smart Stay",
        description_ar="وصف عام اصطناعي.",
        description_en="Synthetic public description.",
        city="Riyadh",
        city_ar="الرياض",
        city_en="Riyadh",
        country_code="SA",
        currency_code="SAR",
        person_capacity=4,
        bedrooms_number=2,
        is_visible=True,
    )


@pytest.fixture
def image(property_obj: Property) -> PropertyImage:
    return PropertyImage.objects.create(
        property=property_obj,
        hostaway_image_id=99000101,
        hostaway_url="https://hostaway-platform.s3.us-west-2.amazonaws.com/synthetic.jpg",
        sync_key="id:99000101",
        source=PropertyImage.Source.HOSTAWAY,
        is_visible=True,
        is_cover=True,
        alt_text_ar="غرفة معيشة اصطناعية",
        alt_text_en="Synthetic living room",
    )


@pytest.fixture
def visible_review(property_obj: Property) -> Review:
    return Review.objects.create(
        hostaway_review_id=990001,
        property=property_obj,
        hostaway_listing_map_id=990001,
        review_type=Review.Type.GUEST_TO_HOST,
        status=Review.Status.PUBLISHED,
        guest_name="Synthetic Guest",
        rating=Decimal("8.0"),
        public_review="Synthetic public review.",
        departure_date=date(2026, 7, 1),
        is_visible=True,
        synced_at=timezone.now(),
    )


@pytest.fixture
def superuser():
    return get_user_model().objects.create_superuser(
        username="phase10-admin",
        email="admin@example.invalid",
        password="synthetic-password",
    )


def test_google_disabled_does_not_render_google_resources(property_obj: Property) -> None:
    content = Client().get(property_obj.get_absolute_url()).content.decode()
    assert "googletagmanager.com" not in content
    assert "google-analytics.com" not in content
    assert 'data-gtm-container-id=""' in content


def test_technical_error_page_excludes_google_when_globally_enabled() -> None:
    with override_settings(
        GOOGLE_INTEGRATIONS_ENABLED=True,
        GOOGLE_TAG_MANAGER_ENABLED=True,
        GOOGLE_TAG_MANAGER_CONTAINER_ID="GTM-ABCD1",
    ):
        content = Client().get("/synthetic-missing-page/").content.decode()
    assert "GTM-ABCD1" not in content
    assert "googletagmanager.com" not in content


@pytest.mark.parametrize(
    ("setting_name", "identifier", "expected_error"),
    [
        ("GOOGLE_TAG_MANAGER_CONTAINER_ID", "invalid", "core.E101"),
        ("GOOGLE_ANALYTICS_MEASUREMENT_ID", "UA-123", "core.E102"),
        ("GOOGLE_ADS_CONVERSION_ID", "ADS-123", "core.E103"),
    ],
)
def test_invalid_google_identifiers(
    setting_name: str,
    identifier: str,
    expected_error: str,
) -> None:
    values = {
        "GOOGLE_INTEGRATIONS_ENABLED": True,
        "GOOGLE_TAG_MANAGER_ENABLED": setting_name == "GOOGLE_TAG_MANAGER_CONTAINER_ID",
        "GOOGLE_ANALYTICS_ENABLED": setting_name == "GOOGLE_ANALYTICS_MEASUREMENT_ID",
        "GOOGLE_ADS_ENABLED": setting_name == "GOOGLE_ADS_CONVERSION_ID",
        "GOOGLE_TAG_MANAGER_CONTAINER_ID": "GTM-ABCD",
        "GOOGLE_ANALYTICS_MEASUREMENT_ID": "G-ABCD",
        "GOOGLE_ADS_CONVERSION_ID": "AW-123456",
        "GOOGLE_ADS_BOOKING_CONVERSION_LABEL": "BOOKING_1",
        "GOOGLE_ADS_CONTACT_CONVERSION_LABEL": "CONTACT_1",
        setting_name: identifier,
    }
    with override_settings(**values):
        assert expected_error in {error_id for error_id, _ in validate_google_configuration()}


def test_valid_google_identifiers_pass_local_validation() -> None:
    with override_settings(
        GOOGLE_INTEGRATIONS_ENABLED=True,
        GOOGLE_TAG_MANAGER_ENABLED=True,
        GOOGLE_TAG_MANAGER_CONTAINER_ID="GTM-ABCD1",
        GOOGLE_ANALYTICS_ENABLED=True,
        GOOGLE_ANALYTICS_MEASUREMENT_ID="G-ABCD1",
        GOOGLE_ADS_ENABLED=True,
        GOOGLE_ADS_CONVERSION_ID="AW-123456",
        GOOGLE_ADS_BOOKING_CONVERSION_LABEL="BOOKING_1",
        GOOGLE_ADS_CONTACT_CONVERSION_LABEL="CONTACT_1",
    ):
        assert validate_google_configuration() == []


def test_enhanced_conversions_are_rejected() -> None:
    with override_settings(
        GOOGLE_INTEGRATIONS_ENABLED=True,
        GOOGLE_ADS_ENHANCED_CONVERSIONS_ENABLED=True,
    ):
        assert "core.E105" in {error_id for error_id, _ in validate_google_configuration()}


def test_invalid_consent_default_is_rejected() -> None:
    with override_settings(
        GOOGLE_INTEGRATIONS_ENABLED=True,
        GOOGLE_CONSENT_DEFAULT_ANALYTICS_STORAGE="maybe",
    ):
        assert "core.E106" in {error_id for error_id, _ in validate_google_configuration()}


def test_consent_defaults_are_denied() -> None:
    content = Client().get("/").content.decode()
    for name in (
        'data-consent-default-analytics="denied"',
        'data-consent-default-ad="denied"',
        'data-consent-default-ad-user-data="denied"',
        'data-consent-default-ad-personalization="denied"',
    ):
        assert name in content


@pytest.mark.parametrize(
    "marker",
    [
        "data-consent-accept",
        "data-consent-reject",
        "data-consent-customize",
        "data-consent-save",
        "data-consent-settings",
        "data-consent-analytics",
        "data-consent-marketing",
    ],
)
def test_consent_controls_are_rendered(marker: str) -> None:
    assert marker in Client().get("/").content.decode()


def test_consent_banner_is_rtl_in_arabic() -> None:
    content = Client().get("/").content.decode()
    assert 'lang="ar" dir="rtl"' in content
    assert "data-consent-banner" in content


def test_consent_banner_is_ltr_in_english() -> None:
    client = Client()
    client.post("/i18n/setlang/", {"language": "en", "next": "/"})
    content = client.get("/").content.decode()
    assert 'lang="en" dir="ltr"' in content
    assert "Your privacy choices" in content


def test_consent_version_is_exposed_without_pii() -> None:
    with override_settings(COOKIE_CONSENT_VERSION=7):
        content = Client().get("/").content.decode()
    assert 'data-cookie-consent-version="7"' in content


def test_consent_cookie_contract_is_privacy_safe() -> None:
    script = Path("static/js/consent.js").read_text(encoding="utf-8")
    assert "samesite=lax" in script
    assert 'attributes.push("secure")' in script
    assert "HttpOnly" not in script
    assert "email" not in script and "phone" not in script


def test_consent_default_precedes_google_loader() -> None:
    script = Path("static/js/consent.js").read_text(encoding="utf-8")
    assert script.index('window.gtag("consent", "default"') < script.index("function loadGoogle")


def test_consent_dialog_has_keyboard_focus_handling() -> None:
    script = Path("static/js/consent.js").read_text(encoding="utf-8")
    assert 'event.key !== "Tab"' in script
    assert 'dialog?.addEventListener("cancel"' in script
    assert "returnFocus" in script


@pytest.mark.parametrize(
    "event_name",
    sorted(EVENT_SCHEMAS),
)
def test_analytics_event_allowlist_accepts_known_events(event_name: str) -> None:
    assert sanitize_analytics_event(event_name, {}) == {}


def test_analytics_rejects_unknown_event() -> None:
    with pytest.raises(ValueError, match="analytics_event_not_allowed"):
        sanitize_analytics_event("unexpected_event", {})


@pytest.mark.parametrize(
    "forbidden_key",
    [
        "email",
        "phone",
        "first_name",
        "last_name",
        "guest_name",
        "special_requests",
        "session_key_hash",
        "hostaway_listing_id",
        "hostaway_listing_map_id",
        "reservation_id",
        "authorization",
    ],
)
def test_analytics_rejects_pii_and_internal_keys(forbidden_key: str) -> None:
    with pytest.raises(ValueError, match="forbidden"):
        sanitize_analytics_event("generate_lead", {forbidden_key: "synthetic"})


def test_analytics_discards_unknown_payload_keys() -> None:
    assert sanitize_analytics_event(
        "availability_result",
        {"available": True, "unknown": "discarded"},
    ) == {"available": True}


def test_analytics_limits_item_count_and_item_fields() -> None:
    payload = sanitize_analytics_event(
        "view_item_list",
        {
            "items": [
                {"item_id": f"stay-{index}", "quantity": 1, "private": "removed"}
                for index in range(30)
            ]
        },
    )
    assert len(payload["items"]) == 20
    assert all("private" not in item for item in payload["items"])


def test_property_analytics_item_uses_slug_not_hostaway_id(property_obj: Property) -> None:
    item = property_analytics_item(property_obj)
    assert item["item_id"] == property_obj.slug
    assert str(property_obj.hostaway_listing_id) not in str(item)


def test_property_list_and_detail_render_safe_events(
    property_obj: Property,
    image: PropertyImage,
) -> None:
    list_content = Client().get("/properties/").content.decode()
    detail_content = Client().get(property_obj.get_absolute_url()).content.decode()
    assert "data-analytics-list" in list_content
    assert "data-analytics-item" in list_content
    assert "data-analytics-view-item" in detail_content
    assert str(property_obj.hostaway_listing_id) not in detail_content


def test_analytics_debug_source_logs_keys_not_payload() -> None:
    script = Path("static/js/analytics.js").read_text(encoding="utf-8")
    assert 'console.info("LSA analytics event", eventName, Object.keys(clean))' in script
    assert "console.info(payload)" not in script


def test_purchase_is_not_prepared_for_unpaid_attempt() -> None:
    attempt = SimpleNamespace(status=PaymentAttempt.Status.PENDING)
    reservation = SimpleNamespace()
    assert prepare_purchase_event(
        payment_attempt=attempt,
        reservation=reservation,
    ) == (None, None)
    assert MarketingEventReceipt.objects.count() == 0


def test_purchase_is_not_prepared_without_confirmed_hostaway_reservation() -> None:
    attempt = SimpleNamespace(status=PaymentAttempt.Status.SUCCEEDED)
    reservation = SimpleNamespace(
        normalized_status=Reservation.Status.AWAITING_PAYMENT,
        hostaway_reservation_id=None,
    )
    assert prepare_purchase_event(
        payment_attempt=attempt,
        reservation=reservation,
    ) == (None, None)
    assert MarketingEventReceipt.objects.count() == 0


def test_refund_receipt_is_not_created_by_public_pages() -> None:
    Client().get("/")
    assert not MarketingEventReceipt.objects.filter(event_name="refund").exists()


def test_sitemap_is_xml_and_contains_public_property(
    property_obj: Property,
    image: PropertyImage,
) -> None:
    response = Client().get("/sitemap.xml")
    content = response.content.decode()
    assert response.status_code == 200
    assert response["Content-Type"].startswith("application/xml")
    assert property_obj.get_absolute_url() in content
    assert 'hreflang="ar"' in content
    assert 'hreflang="en"' in content
    assert 'hreflang="x-default"' in content


def test_sitemap_excludes_hidden_and_archived_properties(property_obj: Property) -> None:
    property_obj.is_visible = False
    property_obj.save()
    cache.clear()
    assert property_obj.get_absolute_url() not in Client().get("/sitemap.xml").content.decode()
    property_obj.is_visible = True
    property_obj.hostaway_special_status = "archived"
    property_obj.save()
    cache.clear()
    assert property_obj.get_absolute_url() not in Client().get("/sitemap.xml").content.decode()


@pytest.mark.parametrize(
    "private_fragment",
    [
        "/admin/",
        "/health/",
        "/integrations/",
        "/reservations/quotes/",
        "/reservations/requests/",
        "/reservations/manage/",
    ],
)
def test_sitemap_excludes_private_routes(private_fragment: str) -> None:
    assert private_fragment not in Client().get("/sitemap.xml").content.decode()


def test_sitemap_contains_no_synthetic_pii(property_obj: Property) -> None:
    content = Client().get("/sitemap.xml").content.decode()
    assert "guest@example.invalid" not in content
    assert "+966500000000" not in content
    assert str(property_obj.hostaway_listing_id) not in content


def test_robots_has_sitemap_and_private_disallows() -> None:
    response = Client().get("/robots.txt")
    content = response.content.decode()
    assert response["Content-Type"].startswith("text/plain")
    assert "Sitemap:" in content
    for path in ("/admin/", "/health/", "/integrations/", "/reservations/quotes/"):
        assert f"Disallow: {path}" in content


def test_public_page_has_canonical_and_hreflang(property_obj: Property) -> None:
    content = Client().get(property_obj.get_absolute_url()).content.decode()
    assert '<link rel="canonical"' in content
    assert 'hreflang="ar"' in content
    assert 'hreflang="en"' in content
    assert 'hreflang="x-default"' in content


def test_filtered_list_canonical_drops_filter_query() -> None:
    content = Client().get("/properties/?city=Riyadh").content.decode()
    assert f'rel="canonical" href="{settings.SITE_CANONICAL_URL}/properties/"' in content
    assert "city=Riyadh" not in content.split('rel="canonical"', 1)[1].split(">", 1)[0]


def test_paginated_list_canonical_keeps_page_number() -> None:
    for index in range(10):
        Property.objects.create(
            hostaway_listing_id=991000 + index,
            slug=f"pagination-stay-{index}",
            hostaway_name=f"Pagination Stay {index}",
            is_visible=True,
        )
    content = Client().get("/properties/?page=2").content.decode()
    assert f"{settings.SITE_CANONICAL_URL}/properties/?page=2" in content


def test_private_quote_template_has_no_hreflang_block() -> None:
    source = Path("templates/reservations/booking_quote_detail.html").read_text(encoding="utf-8")
    assert "{% block robots %}noindex,nofollow{% endblock %}" in source
    assert "{% block hreflang %}{% endblock %}" in source


def test_property_structured_data_is_safe_and_complete(
    property_obj: Property,
    image: PropertyImage,
    visible_review: Review,
) -> None:
    property_obj.address = "PRIVATE SYNTHETIC ADDRESS"
    property_obj.public_address = "Synthetic public district"
    property_obj.save()
    data = property_structured_data(property_obj)
    rendered = str(data)
    assert data["@type"] == "VacationRental"
    assert data["identifier"] == property_obj.slug
    assert data["aggregateRating"]["reviewCount"] == 1
    assert data["aggregateRating"]["ratingValue"] == 4.0
    assert "PRIVATE SYNTHETIC ADDRESS" not in rendered
    assert str(property_obj.hostaway_listing_id) not in rendered
    assert "price" not in data and "availability" not in data


def test_hidden_review_not_counted_in_structured_data(
    property_obj: Property,
    image: PropertyImage,
    visible_review: Review,
) -> None:
    visible_review.is_visible = False
    visible_review.save()
    assert "aggregateRating" not in property_structured_data(property_obj)


def test_detail_metadata_has_open_graph_and_safe_image(
    property_obj: Property,
    image: PropertyImage,
) -> None:
    content = Client().get(property_obj.get_absolute_url()).content.decode()
    assert 'property="og:title"' in content
    assert 'property="og:image"' in content
    assert 'property="og:image:width" content="960"' in content
    assert 'name="twitter:image"' in content


def test_detail_metadata_prefers_local_seo_title(
    property_obj: Property,
    image: PropertyImage,
) -> None:
    property_obj.seo_title_ar = "عنوان SEO اصطناعي"
    property_obj.save()
    content = Client().get(property_obj.get_absolute_url()).content.decode()
    assert "<title>عنوان SEO اصطناعي | Luxury Smart Apartments</title>" in content


@pytest.mark.parametrize("redirect_type", [301, 302])
def test_legacy_redirect_returns_configured_status(redirect_type: int) -> None:
    redirect = LegacyRedirect.objects.create(
        source_path=f"/old-synthetic-{redirect_type}/",
        destination_path="/about/",
        redirect_type=redirect_type,
    )
    response = Client().get(redirect.source_path)
    assert response.status_code == redirect_type
    assert response["Location"] == "/about/"
    redirect.refresh_from_db()
    assert redirect.hit_count == 1
    assert redirect.last_hit_at is not None


def test_legacy_gone_returns_branded_410() -> None:
    redirect = LegacyRedirect.objects.create(
        source_path="/removed-synthetic/",
        redirect_type=LegacyRedirect.RedirectType.GONE,
    )
    response = Client().get(redirect.source_path)
    assert response.status_code == 410
    assert b"410" in response.content
    assert b"noindex,nofollow" in response.content


@pytest.mark.parametrize(
    ("source", "destination"),
    [
        ("https://example.invalid/old", "/about/"),
        ("//example.invalid/old", "/about/"),
        ("/old?secret=value", "/about/"),
        ("/old/", "https://example.invalid/new"),
        ("/same/", "/same/"),
        ("/admin/old/", "/about/"),
        ("/old-admin/", "/admin/"),
        ("/integrations/webhook/old/", "/about/"),
        ("/health/old/", "/about/"),
    ],
)
def test_legacy_redirect_rejects_unsafe_paths(source: str, destination: str) -> None:
    redirect = LegacyRedirect(
        source_path=source,
        destination_path=destination,
        redirect_type=LegacyRedirect.RedirectType.PERMANENT,
    )
    with pytest.raises(ValidationError):
        redirect.full_clean()


def test_legacy_redirect_rejects_two_way_loop() -> None:
    LegacyRedirect.objects.create(
        source_path="/loop-a/",
        destination_path="/loop-b/",
    )
    reverse_redirect = LegacyRedirect(
        source_path="/loop-b/",
        destination_path="/loop-a/",
    )
    with pytest.raises(ValidationError):
        reverse_redirect.full_clean()


def test_legacy_redirect_rejects_indirect_loop() -> None:
    LegacyRedirect.objects.create(
        source_path="/chain-b/",
        destination_path="/chain-c/",
    )
    LegacyRedirect.objects.create(
        source_path="/chain-c/",
        destination_path="/chain-a/",
    )
    redirect = LegacyRedirect(
        source_path="/chain-a/",
        destination_path="/chain-b/",
    )
    with pytest.raises(ValidationError):
        redirect.full_clean()


def test_legacy_redirect_uses_exact_path_only() -> None:
    LegacyRedirect.objects.create(
        source_path="/exact-old/",
        destination_path="/about/",
    )
    assert Client().get("/exact-old/extra/").status_code == 404


def test_seeded_confirmed_redirects_are_available() -> None:
    assert Client().get("/about-us/").status_code == 301
    assert Client().get("/contact-us/").status_code == 301
    assert Client().get("/my-bookings/").status_code == 302


def test_redirect_verification_command_is_read_only() -> None:
    before = LegacyRedirect.objects.count()
    output = StringIO()
    call_command(
        "verify_legacy_redirects",
        "--dry-run",
        "--show-unmatched",
        "--check-loops",
        stdout=output,
    )
    assert LegacyRedirect.objects.count() == before
    assert "manual_mapping_required" in output.getvalue()


def test_marketing_diagnostics_requires_staff() -> None:
    assert Client().get(reverse("marketing:diagnostics")).status_code == 302


def test_marketing_diagnostics_is_local_only(superuser: object) -> None:
    client = Client()
    client.force_login(superuser)
    with patch("httpx.Client.request", side_effect=AssertionError("no network")):
        response = client.get(reverse("marketing:diagnostics"))
    assert response.status_code == 200
    assert b"Google" in response.content


def test_seo_dashboard_requires_staff() -> None:
    assert Client().get(reverse("marketing:seo_dashboard")).status_code == 302


def test_seo_dashboard_uses_postgresql_only(superuser: object) -> None:
    client = Client()
    client.force_login(superuser)
    with patch("httpx.Client.request", side_effect=AssertionError("no network")):
        response = client.get(reverse("marketing:seo_dashboard"))
    assert response.status_code == 200


@pytest.mark.parametrize(
    "component",
    ["event", "structured_data", "redirects", "sitemap"],
)
def test_local_diagnostic_actions_are_csrf_protected(
    superuser: object,
    component: str,
) -> None:
    client = Client(enforce_csrf_checks=True)
    client.force_login(superuser)
    response = client.post(reverse("marketing:validate", args=[component]))
    assert response.status_code == 403


def test_csp_has_no_general_unsafe_inline() -> None:
    response = Client().get("/")
    csp = response["Content-Security-Policy"]
    assert "script-src 'self'" in csp
    assert "'unsafe-eval'" not in csp
    assert "script-src 'self' 'unsafe-inline'" not in csp


def test_google_domains_are_absent_from_disabled_csp() -> None:
    csp = Client().get("/")["Content-Security-Policy"]
    assert "googletagmanager.com" not in csp
    assert "google-analytics.com" not in csp
    assert "doubleclick.net" not in csp


def test_public_page_load_does_not_create_commercial_records() -> None:
    counts = (
        Reservation.objects.count(),
        PaymentAttempt.objects.count(),
        MarketingEventReceipt.objects.count(),
    )
    with patch("httpx.Client.request", side_effect=AssertionError("no network")):
        Client().get("/")
        Client().get("/properties/")
    assert counts == (
        Reservation.objects.count(),
        PaymentAttempt.objects.count(),
        MarketingEventReceipt.objects.count(),
    )
