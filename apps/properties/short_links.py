"""Fixed, localized catalogue links; never a general URL shortener."""

from types import MappingProxyType

from django.http import Http404, HttpRequest, HttpResponseRedirect
from django.shortcuts import get_object_or_404
from django.urls import reverse
from django.views.decorators.http import require_safe

from .models import Property

# Stable Hostaway listing IDs, shared with the existing property catalogue.
# Deliberately excludes the hidden Al Hamra villa and arbitrary property slugs.
CATALOGUE_LISTING_IDS = MappingProxyType(
    {
        "e12": 315816,
        "irqah": 315814,
        "darat-safa": 315815,
        "a11": 325961,
        "b12": 343666,
        "nour-prestige": 511786,
    }
)


@require_safe
def catalogue_short_link(request: HttpRequest, code: str, *, photos: bool = False):
    listing_id = CATALOGUE_LISTING_IDS.get(code)
    if listing_id is None:
        raise Http404
    apartment = get_object_or_404(
        Property.objects.public().only("slug"), hostaway_listing_id=listing_id
    )
    # LocaleMiddleware takes the language from the explicit /ar/, /en/ or /fr/ path.
    # Query parameters never select a target or get copied into the destination.
    destination = reverse(
        "properties:gallery" if photos else "properties:detail", kwargs={"slug": apartment.slug}
    )
    if not photos:
        destination += "#price-calendar"
    response = HttpResponseRedirect(destination)
    # Recheck visibility and the canonical slug on every visit, including after renames.
    response["Cache-Control"] = "no-store"
    response["X-Robots-Tag"] = "noindex, follow"
    return response
