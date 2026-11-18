"""Consent-gated, bounded correlation for public search measurement; no provider calls."""

import hashlib
import json
import re
import time
from uuid import uuid4

from django.utils.crypto import salted_hmac

VERSION = "pp1"
TTL = 1800
PAGES = {"home", "property_list", "property_detail", "availability_results", "quote", "unknown"}
TECHNICAL_REASONS = {
    "hostaway_temporarily_unavailable",
    "pricing_unavailable",
    "calendar_incomplete",
}
DISPLAY = {
    "available": "available",
    "unavailable": "unavailable",
    "no_results": "no_results",
    "partial_error": "partial_results",
    "upstream_error": "verification_error",
    "invalid_request": "invalid_request",
    "rate_limited": "rate_limited",
    "quote_error": "quote_error",
}


def page_kind(request):
    name = getattr(getattr(request, "resolver_match", None), "view_name", "")
    return {
        "core:home": "home",
        "properties:list": "property_list",
        "properties:detail": "property_detail",
        "reservations:quote_create": "availability_results",
        "legacy_search_availability": "availability_results",
        "reservations:quote_detail": "quote",
        "reservations:guest_details": "quote",
    }.get(name, "unknown")


def city_code(value):
    value = str(value or "").lower()
    if not value:
        return "all"
    if "riyadh" in value:
        return "riyadh"
    if "marrak" in value:
        return "marrakech"
    return "other"


def property_context(property_obj):
    from apps.core.templatetags.presentation import localized_property_name

    return {
        "property_id": property_obj.slug,
        "property_name": localized_property_name(property_obj),
    }


def technical_failure(result):
    return result.reason_code in TECHNICAL_REASONS or (result.is_available and result.quote is None)


def _consented(request):
    from apps.core.context_processors import _consent_from_cookie

    choice = _consent_from_cookie(request)
    return bool(choice and choice["analytics"] is True)


def _criteria(data):
    return hashlib.sha256(
        json.dumps(
            {key: str(data.get(key, "")) for key in ("city", "check_in", "check_out", "guests")},
            sort_keys=True,
        ).encode()
    ).hexdigest()


def attempt_context(request, form=None):
    data = getattr(form, "cleaned_data", {}) or {}
    source = request.POST if request.method == "POST" else request.GET
    kind = "property_quote" if source.get("property") else "portfolio"
    entry = source.get("measurement_entry", "unknown")
    context = {
        "measurement_version": VERSION,
        "attempt_id": uuid4().hex,
        "request_kind": kind,
        "entry_page": entry if entry in PAGES else "unknown",
        "page_kind": "availability_results",
        "stay_kind": "overnight",
    }
    token = source.get("measurement_attempt", "")
    if _consented(request) and re.fullmatch(r"[a-f0-9]{32}", token):
        context["attempt_id"] = token
    parent = source.get("measurement_parent", "")
    if _consented(request) and re.fullmatch(r"[a-f0-9]{32}", parent):
        context["parent_attempt_id"] = parent
    journey = source.get("measurement_journey", context["entry_page"])
    context["journey_entry"] = journey if journey in PAGES else "unknown"
    context["city"] = city_code(data.get("city"))
    if data.get("check_in") and data.get("check_out"):
        nights = (data["check_out"] - data["check_in"]).days
        if 0 < nights <= 3660:
            context["nights"] = nights
    if isinstance(data.get("guests"), int) and 0 < data["guests"] <= 100:
        context["guests"] = data["guests"]
    if request.method == "GET" and kind == "portfolio" and _consented(request):
        previous = request.session.get("pp1_portfolio", {})
        if (
            isinstance(previous, dict)
            and previous.get("criteria") == _criteria(data)
            and isinstance(previous.get("at"), int | float)
            and time.time() - previous["at"] < TTL
        ):
            context.update(previous.get("context", {}))
    return context


def result_context(context, status, *, count=None, checked=None, errors=None, category="none"):
    result = {
        **context,
        "result_status": status,
        "display_status": DISPLAY[status],
        "error_category": category,
    }
    for key, value in (
        ("result_count", count),
        ("checked_count", checked),
        ("error_count", errors),
    ):
        if value is not None:
            result[key] = value
    return result


def remember_portfolio(request, form, context):
    if _consented(request):
        request.session["pp1_portfolio"] = {
            "criteria": _criteria(form.cleaned_data),
            "at": time.time(),
            "context": context,
        }


def remember_quote(request, quote, context):
    if not _consented(request):
        return
    now = time.time()
    previous = request.session.get("pp1_quotes", {})
    previous = previous if isinstance(previous, dict) else {}
    entries = {
        key: value
        for key, value in previous.items()
        if isinstance(value, dict) and now - value.get("at", 0) < TTL
    }
    entries[str(quote.pk)] = {"at": now, "context": context}
    request.session["pp1_quotes"] = dict(list(entries.items())[-20:])


def quote_context(request, quote, *, usable):
    # This is a non-exported local dedup key, not an analytics/authorization identity.
    surface = salted_hmac("pp1.quote-surface", str(quote.pk)).hexdigest()[:32]
    context = {
        "measurement_version": VERSION,
        "request_kind": "property_quote",
        "entry_page": "unknown",
        "page_kind": "quote",
        "journey_entry": "unknown",
        "stay_kind": "overnight",
        "surface_key": surface,
    }
    if request is not None and _consented(request):
        entries = request.session.get("pp1_quotes", {})
        entry = entries.get(str(quote.pk), {}) if isinstance(entries, dict) else {}
        if isinstance(entry, dict) and time.time() - entry.get("at", 0) < TTL:
            context.update(entry.get("context", {}))
            context["page_kind"] = "quote"
    if not usable:
        return {"prepurchase_context": context}
    result = context if context.get("result_status") else None
    return {"prepurchase_context": context, "prepurchase_result": result}
