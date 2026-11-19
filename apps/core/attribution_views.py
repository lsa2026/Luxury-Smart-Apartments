"""Same-origin consent updates and a configured WhatsApp transition only."""

import logging
import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from django.core.cache import cache
from django.db import DatabaseError
from django.http import HttpResponseRedirect, JsonResponse
from django.urls import reverse
from django.utils import translation
from django.views.decorators.http import require_GET, require_POST

from . import assisted_attribution as attribution
from .context_processors import _whatsapp_contact
from .models import SiteSetting

logger = logging.getLogger(__name__)


def transition_url(language):
    language = language if language in {"ar", "en", "fr"} else "ar"
    return reverse("marketing:whatsapp_transition") + "?language=" + language


@require_POST
def attribution_consent(request):
    if len(request.body) > 4096:
        return JsonResponse({"state": "invalid"}, status=400)
    if not attribution.consent(request):
        attribution.withdraw(request)
        return JsonResponse({"state": "cleared"})
    if request.POST.get("page_kind") not in {"home", "property_list", "property_detail", "contact"}:
        return JsonResponse({"state": "ignored"})
    slug = request.POST.get("property_id", "")
    if slug:
        from apps.properties.models import Property

        if not Property.objects.filter(slug=slug, is_visible=True).exists():
            slug = ""
    attribution.capture(request, request.POST, property_slug=slug)
    return JsonResponse(
        {"state": "captured", "whatsapp_url": transition_url(request.LANGUAGE_CODE)}
    )


@require_GET
def whatsapp_transition(request):
    language = request.GET.get("language", "ar")
    language = language if language in {"ar", "en", "fr"} else "ar"
    with translation.override(language):
        contact = _whatsapp_contact(SiteSetting.objects.first())
    target = contact["url"]
    parts = urlsplit(target)
    if parts.scheme != "https" or parts.hostname not in {"wa.me", "api.whatsapp.com"}:
        return JsonResponse({"state": "unavailable"}, status=503)
    # A short business link cannot safely carry a new text. Preserve it unchanged.
    can_append = (parts.hostname == "wa.me" and re.fullmatch(r"/[0-9]+", parts.path)) or (
        parts.hostname == "api.whatsapp.com" and parts.path == "/send"
    )
    if can_append and attribution.context(request):
        try:
            rate_key = "aa1-transition:" + attribution.context(request)["journey_hash"]
            cache.add(rate_key, 0, timeout=3600)
            count = cache.incr(rate_key)
            code = attribution.handoff(request) if count <= 30 else ""
        except (DatabaseError, ValueError):
            logger.warning("Optional advertising handoff unavailable")
            code = ""
        if code:
            params = dict(parse_qsl(parts.query, keep_blank_values=True))
            label = {
                "ar": "مرجع الاستفسار",
                "en": "Enquiry reference",
                "fr": "Référence de demande",
            }[language]
            params["text"] = (params.get("text", "") + "\n" + label + ": " + code).strip()
            target = urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(params), ""))
    response = HttpResponseRedirect(target)
    response["Cache-Control"] = "private, no-store"
    response["Referrer-Policy"] = "no-referrer"
    response["X-Robots-Tag"] = "noindex, nofollow"
    return response


class AttributionCaptureMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        match = getattr(request, "resolver_match", None)
        if (
            request.method == "GET"
            and response.status_code == 200
            and match
            and match.view_name in attribution.PUBLIC_NAMES
        ):
            if attribution.consent(request):
                attribution.capture(
                    request, request.GET, property_slug=match.kwargs.get("slug", "")
                )
        return response
