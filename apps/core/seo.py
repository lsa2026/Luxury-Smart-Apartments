"""Local-only technical SEO documents and diagnostics."""

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from html import escape
from typing import Any

from django.conf import settings
from django.core.cache import cache
from django.db.models import Avg, Count, Exists, OuterRef, Q
from django.http import HttpRequest, HttpResponse
from django.urls import Resolver404, resolve, reverse
from django.utils import translation

from apps.properties.models import Property, PropertyImage
from apps.reviews.models import Review

from .models import LegacyRedirect, SitePage

PUBLIC_STATIC_NAMES = (
    "core:home",
    "properties:list",
    "core:about",
    "core:faq",
    "core:contact",
    "core:terms",
    "core:privacy",
    "core:cancellation",
    "core:cookies",
)


@dataclass(frozen=True, slots=True)
class SitemapEntry:
    location: str
    last_modified: datetime | None = None


def public_sitemap_entries() -> list[SitemapEntry]:
    base_url = settings.SITE_CANONICAL_URL.rstrip("/")
    page_updates = dict(SitePage.objects.values_list("slug", "updated_at"))
    name_slug = {
        "core:about": "about",
        "core:terms": "terms",
        "core:privacy": "privacy",
        "core:cancellation": "cancellation",
        "core:cookies": "cookies",
    }
    entries = [
        SitemapEntry(
            f"{base_url}{reverse(name)}",
            page_updates.get(name_slug.get(name, "")),
        )
        for name in PUBLIC_STATIC_NAMES
    ]
    entries.extend(
        SitemapEntry(f"{base_url}{property_obj.get_absolute_url()}", property_obj.updated_at)
        for property_obj in Property.objects.public().only("slug", "updated_at")
    )
    unique = {entry.location: entry for entry in entries}
    return list(unique.values())


def _sitemap_xml(entries: Iterable[SitemapEntry]) -> str:
    rows = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9" '
        'xmlns:xhtml="http://www.w3.org/1999/xhtml">',
    ]
    for entry in entries:
        location = escape(entry.location, quote=True)
        rows.extend(
            [
                "<url>",
                f"<loc>{location}</loc>",
                f'<xhtml:link rel="alternate" hreflang="ar" href="{location}" />',
                f'<xhtml:link rel="alternate" hreflang="en" href="{location}" />',
                f'<xhtml:link rel="alternate" hreflang="x-default" href="{location}" />',
            ]
        )
        if entry.last_modified:
            rows.append(f"<lastmod>{entry.last_modified.date().isoformat()}</lastmod>")
        rows.append("</url>")
    rows.append("</urlset>")
    return "\n".join(rows)


def sitemap_xml(request: HttpRequest) -> HttpResponse:
    del request
    content = cache.get("seo:sitemap:v1")
    if content is None:
        content = _sitemap_xml(public_sitemap_entries())
        cache.set("seo:sitemap:v1", content, timeout=settings.SITEMAP_CACHE_SECONDS)
    return HttpResponse(content, content_type="application/xml; charset=utf-8")


def robots_txt(request: HttpRequest) -> HttpResponse:
    del request
    base_url = settings.SITE_CANONICAL_URL.rstrip("/")
    content = "\n".join(
        [
            "User-agent: *",
            "Allow: /",
            "Disallow: /admin/",
            "Disallow: /integrations/",
            "Disallow: /health/",
            "Disallow: /reservations/quotes/",
            "Disallow: /reservations/requests/",
            "Disallow: /reservations/manage/",
            "Disallow: /*?",
            f"Sitemap: {base_url}/sitemap.xml",
            "",
        ]
    )
    return HttpResponse(content, content_type="text/plain; charset=utf-8")


def seo_diagnostics() -> dict[str, Any]:
    visible_image = PropertyImage.objects.public().filter(property_id=OuterRef("pk"))
    properties = Property.objects.public().annotate(has_public_image=Exists(visible_image))
    missing_alt = (
        PropertyImage.objects.public().filter(Q(alt_text_ar="") | Q(alt_text_en="")).count()
    )
    broken_redirects = LegacyRedirect.objects.filter(
        is_active=True,
    ).exclude(redirect_type=LegacyRedirect.RedirectType.GONE)
    broken_count = sum(
        1
        for redirect in broken_redirects.only("destination_path")
        if not _resolves_publicly(redirect.destination_path)
    )
    return {
        "missing_seo_ar": properties.filter(seo_title_ar="").count(),
        "missing_seo_en": properties.filter(seo_title_en="").count(),
        "missing_description": properties.filter(
            description_ar="",
            description_en="",
            hostaway_description="",
        ).count(),
        "missing_image": properties.filter(has_public_image=False).count(),
        "missing_alt": missing_alt,
        "structured_ready": properties.filter(has_public_image=True)
        .exclude(Q(name_ar="") & Q(name_en="") & Q(hostaway_name=""))
        .count(),
        "redirect_loops": sum(
            1
            for redirect in LegacyRedirect.objects.filter(is_active=True).exclude(
                destination_path=""
            )
            if LegacyRedirect.objects.filter(
                source_path=redirect.destination_path,
                destination_path=redirect.source_path,
                is_active=True,
            ).exists()
        ),
        "broken_redirect_destinations": broken_count,
        "sitemap_urls": len(public_sitemap_entries()),
        "private_noindex_groups": 5,
        "missing_languages": properties.filter(Q(name_ar="") | Q(name_en="")).count(),
    }


def _resolves_publicly(path: str) -> bool:
    if not path or path.startswith(LegacyRedirect.PROTECTED_PREFIXES):
        return False
    try:
        match = resolve(path)
    except Resolver404:
        return False
    if match.route.startswith(("admin/", "integrations/", "health/")):
        return False
    if match.view_name == "properties:detail":
        return Property.objects.public().filter(slug=match.kwargs.get("slug", "")).exists()
    return True


def property_structured_data(property_obj: Property) -> dict[str, Any]:
    prefetched_images = getattr(property_obj, "_public_images", None)
    if prefetched_images is None:
        prefetched_images = list(
            property_obj.images.public().order_by("-is_cover", "sort_order", "id")[:5]
        )
    image_urls = [image.display_url for image in prefetched_images[:5] if image.display_url]
    language = (translation.get_language() or "ar").split("-")[0]
    description_fields = {
        "ar": ("description_ar", "description_en", "hostaway_description"),
        "en": ("description_en", "hostaway_description", "description_ar"),
    }.get(language, ("description_ar", "description_en", "hostaway_description"))
    description = next(
        (
            value
            for field_name in description_fields
            if (value := getattr(property_obj, field_name, ""))
        ),
        "",
    )[:500]
    data: dict[str, Any] = {
        "@context": "https://schema.org",
        "@type": "VacationRental" if description and image_urls else "LodgingBusiness",
        "name": property_obj.display_name,
        "description": description,
        "url": f"{settings.SITE_CANONICAL_URL}{property_obj.get_absolute_url()}",
        "identifier": property_obj.slug,
    }
    if image_urls:
        data["image"] = image_urls
    locality = property_obj.display_city
    if locality or property_obj.country_code:
        data["address"] = {
            "@type": "PostalAddress",
            **({"addressLocality": locality} if locality else {}),
            **({"addressCountry": property_obj.country_code} if property_obj.country_code else {}),
        }
    if property_obj.person_capacity:
        data["occupancy"] = {
            "@type": "QuantitativeValue",
            "maxValue": property_obj.person_capacity,
        }
    if property_obj.bedrooms_number is not None:
        data["numberOfBedrooms"] = property_obj.bedrooms_number
    amenities = [
        link.amenity.display_name
        for link in getattr(property_obj, "_public_amenities", [])
        if link.amenity.display_name
    ]
    if amenities:
        data["amenityFeature"] = [
            {"@type": "LocationFeatureSpecification", "name": name, "value": True}
            for name in amenities[:20]
        ]
    rating = (
        Review.objects.public()
        .filter(property=property_obj)
        .aggregate(
            average=Avg("rating"),
            count=Count("id"),
        )
    )
    if rating["average"] is not None and rating["count"]:
        data["aggregateRating"] = {
            "@type": "AggregateRating",
            "ratingValue": round(float(rating["average"]) / 2, 1),
            "bestRating": 5,
            "worstRating": 0,
            "reviewCount": rating["count"],
        }
    return data
