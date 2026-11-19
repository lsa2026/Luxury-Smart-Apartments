"""Consented link context, explicit private handoff and purchase enrichment only."""

import copy
import json
import re
import secrets
from datetime import datetime, timedelta
from urllib.parse import unquote

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone
from django.utils.crypto import salted_hmac

from .attribution_models import AttributionHandoff, BookingAttribution

SESSION_KEY = "advertising_context_aa1"
TTL = timedelta(days=30)
PUBLIC_NAMES = {"core:home", "core:contact", "properties:list", "properties:detail"}
STAFF_SOURCES = {"unknown", "google_ads", "social", "referral", "returning", "other"}
SOURCES = {
    "google",
    "bing",
    "facebook",
    "instagram",
    "tiktok",
    "snapchat",
    "whatsapp",
    "email",
    "referral",
    "unknown",
}
MEDIUMS = {
    "cpc",
    "ppc",
    "paid_social",
    "social",
    "organic",
    "email",
    "referral",
    "messaging",
    "none",
    "unknown",
}
KNOWN_CAMPAIGNS = {"24262302633", "24262298844", "24426104371"}
FIELDS = (
    "attribution_version",
    "attribution_status",
    "closure_method",
    "acquisition_kind",
    "first_source",
    "first_medium",
    "first_campaign_id",
    "last_source",
    "last_medium",
    "last_campaign_id",
    "campaign_evidence",
    "origin_property_id",
    "market",
)
ENUMS = {
    "attribution_version": {"aa1"},
    "attribution_status": {"reference_linked", "session_linked", "staff_reported", "unknown"},
    "closure_method": {"self_service", "admin_whatsapp", "admin_other"},
    "acquisition_kind": {
        "new_booking",
        "stay_extension",
        "payment_revision",
        "partial_payment",
        "test",
        "unknown",
    },
    "campaign_evidence": {"tagged_id", "click_only", "none", "staff_reported"},
    "market": {"riyadh", "marrakech", "other"},
}


def consent(request):
    from .context_processors import _consent_from_cookie

    choice = _consent_from_cookie(request)
    return bool(
        choice
        and choice["analytics"]
        and not getattr(getattr(request, "user", None), "is_staff", False)
    )


def _digest(kind, value):
    return salted_hmac(f"aa1.{kind}", value).hexdigest()


def _slug(value, limit=200):
    value = str(value or "")
    return value if re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", value) and len(value) <= limit else ""


def _campaign_id(value):
    if not isinstance(value, str):
        return ""
    return value if re.fullmatch(r"[1-9][0-9]{0,19}", value) and value in KNOWN_CAMPAIGNS else ""


def context(request):
    data = request.session.get(SESSION_KEY, {})
    try:
        expires = datetime.fromisoformat(data["expires_at"])
        valid = timezone.is_aware(expires) and timezone.now() < expires
    except (KeyError, TypeError, ValueError):
        valid = False
    return (
        data
        if valid
        and consent(request)
        and data.get("consent", {}).get("version") == settings.COOKIE_CONSENT_VERSION
        else {}
    )


def capture(request, params, *, property_slug=""):
    """Never retain a full URL, click/browser id, keyword, guest field or referrer."""
    if not consent(request):
        return False
    now = timezone.now()
    data = context(request)
    campaign_id = _campaign_id(params.get("utm_id")) or _campaign_id(params.get("gad_campaignid"))
    source = str(params.get("utm_source", "")).lower()
    medium = str(params.get("utm_medium", "")).lower()
    source = source if source in SOURCES else "unknown"
    medium = medium if medium in MEDIUMS else "unknown"
    click_seen = params.get("click_id_seen") == "1" or any(
        bool(params.get(key)) for key in ("gclid", "gbraid", "wbraid")
    )
    tagged = bool(campaign_id or source != "unknown" or medium != "unknown" or click_seen)
    if data and not tagged:
        return True
    touch = {
        "source": source.lower() or "unknown",
        "medium": medium.lower() or "unknown",
        "campaign_id": campaign_id,
        "campaign_known": campaign_id in KNOWN_CAMPAIGNS,
        "campaign_evidence": "tagged_id" if campaign_id else "click_only" if click_seen else "none",
        "click_id_seen": click_seen,
        "property_id": _slug(property_slug),
        "captured_at": now.isoformat(),
    }
    raw = json.loads(unquote(request.COOKIES["lsa_cookie_consent"]))
    try:
        choice_at = datetime.fromisoformat(str(raw.get("timestamp", "")))
        choice_at = choice_at.isoformat() if timezone.is_aware(choice_at) else ""
    except ValueError:
        choice_at = ""
    if not data:
        data = {
            "journey_hash": _digest("journey", secrets.token_hex(16)),
            "first": touch,
            "expires_at": (now + TTL).isoformat(),
            "consent": {
                "analytics": True,
                "version": settings.COOKIE_CONSENT_VERSION,
                "recorded_at": now.isoformat(),
                "choice_at": choice_at,
            },
        }
    if tagged and not data.get("origin_captured"):
        # The first tagged entry may be the home page: preserve that unknown property.
        data["origin_property_id"] = touch["property_id"]
        data["origin_captured"] = True
    data["last"] = touch
    request.session[SESSION_KEY] = data
    return True


def withdraw(request):
    data = request.session.pop(SESSION_KEY, {})
    journey = data.get("journey_hash") if isinstance(data, dict) else ""
    if journey:
        now = timezone.now()
        AttributionHandoff.objects.filter(journey_hash=journey, revoked_at=None).update(
            revoked_at=now
        )
        BookingAttribution.objects.filter(journey_hash=journey, revoked_at=None).update(
            revoked_at=now
        )


def handoff(request):
    data = context(request)
    if not data:
        return ""
    code = "LSA-" + secrets.token_hex(16).upper()
    AttributionHandoff.objects.create(
        reference_hash=_digest("handoff", code),
        journey_hash=data["journey_hash"],
        snapshot=copy.deepcopy(data),
        expires_at=min(timezone.now() + TTL, datetime.fromisoformat(data["expires_at"])),
    )
    return code


def lookup(code):
    code = str(code or "").strip().upper()
    if not re.fullmatch(r"LSA-[A-F0-9]{32}", code):
        raise ValidationError("تحقق من مرجع واتساب؛ لا تستخدم رقم الجوال أو رقم الحجز.")
    row = AttributionHandoff.objects.filter(reference_hash=_digest("handoff", code)).first()
    if row is None or row.revoked_at or row.expires_at <= timezone.now():
        raise ValidationError("مرجع واتساب غير صالح أو انتهت صلاحيته؛ لم يُثبت مصدر الإعلان.")
    return row


def anchor_fields(anchor):
    return {"BookingQuote": "quote", "MorningStay": "morning_stay", "DayUseStay": "day_use_stay"}[
        type(anchor).__name__
    ]


def bind_admin(anchor, *, code="", staff_source="unknown", actor=None):
    key = anchor_fields(anchor)
    if staff_source not in STAFF_SOURCES:
        raise ValidationError("مصدر الموظف غير معتمد.")
    with transaction.atomic():
        row = lookup(code) if code else None
        if row:
            row = AttributionHandoff.objects.select_for_update().get(pk=row.pk)
            if row.revoked_at or row.expires_at <= timezone.now():
                raise ValidationError("لم يعد مرجع واتساب صالحًا.")
            previous = BookingAttribution.objects.filter(handoff=row).first()
            if previous and getattr(previous, key + "_id") != anchor.pk:
                raise ValidationError("استُخدم المرجع لطلب آخر؛ لا يمكن نقله إلى ضيف آخر.")
            if row.created_at > anchor.created_at:
                raise ValidationError("المرجع أحدث من إنشاء الطلب؛ أعد إنشاء الطلب بالمصدر الصحيح.")
        existing = BookingAttribution.objects.filter(**{key: anchor}).first()
        if existing:
            if not code and staff_source == "unknown":
                return existing
            if (
                existing.handoff_id != (row.pk if row else None)
                or existing.staff_source != staff_source
            ):
                raise ValidationError("مصدر الطلب محفوظ؛ لا يُستبدل ضمن اعتماد بيانات الضيف.")
            return existing
        return BookingAttribution.objects.create(
            **{key: anchor},
            handoff=row,
            journey_hash=row.journey_hash if row else "",
            snapshot=copy.deepcopy(row.snapshot) if row else {},
            linkage="reference_linked"
            if row
            else "staff_reported"
            if staff_source != "unknown"
            else "unknown",
            closure_method="admin_whatsapp" if row else "admin_other",
            staff_source=staff_source,
            recorded_by=actor,
        )


def bind_session(request, quote):
    data = context(request)
    if data:
        BookingAttribution.objects.get_or_create(
            quote=quote,
            defaults={
                "snapshot": copy.deepcopy(data),
                "journey_hash": data["journey_hash"],
                "linkage": "session_linked",
                "closure_method": "self_service",
            },
        )


def purchase_fields(request, attempt, reservation):
    """Called only by paid-event preparation; does not create or emit a purchase."""
    from .context_processors import _consent_from_cookie

    if (
        request is None
        or getattr(getattr(request, "user", None), "is_staff", False)
        or not (_consent_from_cookie(request) or {}).get("analytics")
    ):
        return {}
    if (
        settings.HYPERPAY_ENVIRONMENT != "production"
        or attempt.provider != "hyperpay"
        or not attempt.verified_at
    ):
        return {}
    binding = BookingAttribution.objects.filter(quote_id=attempt.booking_intent.quote_id).first()
    place = reservation.property or attempt.booking_intent.property
    city = " ".join(
        str(getattr(place, key, "") or "").lower() for key in ("city", "city_en", "city_ar")
    )
    market = (
        "riyadh"
        if "riyadh" in city or "الرياض" in city
        else "marrakech"
        if "marrak" in city or "مراكش" in city
        else "other"
    )
    kind = (
        "test"
        if reservation.is_test
        else "stay_extension"
        if hasattr(reservation, "stay_extension")
        else "payment_revision"
        if attempt.modification_request_id
        or (attempt.payment_request_id and attempt.payment_request.modification_request_id)
        else "partial_payment"
        if attempt.payment_request_id
        and attempt.amount != attempt.booking_intent.payment_amount_sar
        else "new_booking"
    )
    result = {
        "attribution_version": "aa1",
        "attribution_status": "unknown",
        "closure_method": "self_service",
        "acquisition_kind": kind,
        "market": market,
        "campaign_evidence": "none",
    }
    if not binding:
        return result
    if binding.revoked_at:
        return {}
    result.update(attribution_status=binding.linkage, closure_method=binding.closure_method)
    data = binding.snapshot
    if binding.linkage == "staff_reported":
        result["campaign_evidence"] = "staff_reported"
    if (
        not data.get("consent", {}).get("analytics")
        or data.get("consent", {}).get("version") != settings.COOKIE_CONSENT_VERSION
    ):
        return result
    for prefix in ("first", "last"):
        touch = data.get(prefix, {})
        for key in ("source", "medium", "campaign_id"):
            if touch.get(key):
                result[f"{prefix}_{key}"] = touch[key]
    last = data.get("last", {})
    result["campaign_evidence"] = last.get("campaign_evidence", "none")
    origin = data.get("origin_property_id")
    if origin:
        result["origin_property_id"] = origin
    return result
