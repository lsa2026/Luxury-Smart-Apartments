"""Optional paid-only enrollment, using the shared first-party invitation registry."""

import hashlib
import json

from django.conf import settings
from django.core.exceptions import ValidationError
from django.http import JsonResponse
from django.utils import timezone
from django.views.decorators.cache import never_cache
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from apps.loyalty import invitation_registry as registry
from apps.loyalty.enrollment_invitation import QUALIFIED_COPY, invitation_caption, invitation_image
from apps.loyalty.invitation_api import ready
from apps.notifications.services.ultramsg import (
    UltraMsgClient,
    UltraMsgConfigurationError,
    UltraMsgResponseError,
)

from .models import Morning07DeliveryAttempt, MorningStay
from .morning07_gateway import authorize, exact_receipt_messages


def caption(stay):
    lang = stay.guest_language
    # This is a website morning stay, not an external-platform past-stay award.
    url = settings.SITE_BASE_URL.rstrip("/") + "/my-bookings/rewards/"
    return invitation_caption(lang, url, stay.guest_name).replace(
        QUALIFIED_COPY[lang][2] + "\n\n", ""
    )


def core_delivered(stay, revision):
    phone = "".join(c for c in stay.guest_phone if c.isdigit())
    stamp = int(stay.starts_at.timestamp() * 1000)
    guide = f"guide:{stay.property.hostaway_listing_map_id}:{stamp}:{phone}"
    attempts = []
    for suffix in (f"r{revision}:status", "rules:" + phone, guide):
        reference = (
            "lsa07-"
            + hashlib.md5(f"lsa07:{stay.pk}:{suffix}".encode(), usedforsecurity=False).hexdigest()
        )
        attempt = Morning07DeliveryAttempt.objects.filter(
            pk=reference, stay=stay, state="accepted"
        ).first()
        if not attempt:
            return False
        attempts.append(attempt)
    for attempt in attempts:
        # Access instructions, as well as status and rules, must already be delivered.
        rows = exact_receipt_messages(attempt)
        if not any(
            str(row.get("ack", "")).lower()
            in {"2", "3", "4", "device", "delivered", "read", "played"}
            or str(row.get("status", "")).lower() in {"delivered", "read", "played"}
            for row in rows
        ):
            return False
    return True


@csrf_exempt
@require_POST
@never_cache
def invite(request):
    stay, revision = authorize(request)
    if len(request.body) > 1024:
        return JsonResponse({"code": "invalid_request"}, status=400)
    try:
        body = json.loads(request.body)
    except (ValueError, TypeError):
        return JsonResponse({"code": "invalid_request"}, status=400)
    if (
        not isinstance(body, dict)
        or set(body) != {"core_delivered"}
        or type(body["core_delivered"]) is not bool
    ):
        return JsonResponse({"code": "invalid_request"}, status=400)
    if stay.status != "paid" or stay.ends_at <= timezone.now():
        return JsonResponse({"code": "not_eligible"})
    if not body["core_delivered"]:
        return JsonResponse({"code": "core_pending"})
    if not ready():
        return JsonResponse({"code": "programme_unavailable"})
    phone = registry.normalize_phone(stay.guest_phone)
    key = "morning07:" + str(stay.pk)
    with registry.invitation_lock():
        if registry.member_exists(phone, stay.guest_email):
            return JsonResponse({"code": "member"})
        prior = registry.competing(key, phone, stay.guest_email)
        if prior:
            return JsonResponse({"code": "duplicate", "status": prior.state})
    try:
        if not core_delivered(stay, revision):
            return JsonResponse({"code": "core_pending"})
        content, text = invitation_image(stay.guest_language), caption(stay)
        with registry.invitation_lock():
            stay = MorningStay.objects.select_for_update().get(pk=stay.pk)
            if stay.communication_revision != revision or stay.status != "paid":
                return JsonResponse({"code": "stale"})
            decision, row = registry.claim(
                key=key,
                phone=phone,
                email=stay.guest_email,
                customer_id=None,
                language=stay.guest_language,
                event_id=key + ":paid-invitation-v1",
                source="morning07",
                origin="website",
                channel="whatsapp",
            )
            if decision != "eligible":
                return JsonResponse({"code": decision})
            if registry.consume(row, phone=phone, morning_stay=stay) != "eligible":
                return JsonResponse({"code": "duplicate"})
        try:
            with UltraMsgClient() as provider:
                result = provider.send_image(
                    recipient=phone,
                    content=content,
                    caption=text,
                    reference_id=row.provider_reference,
                )
            state, provider_id = "accepted", str(result.get("id") or "")
        except (UltraMsgConfigurationError, UltraMsgResponseError):
            state, provider_id = "failed", ""
        except Exception:
            state, provider_id = "unknown", ""
        with registry.invitation_lock():
            row.refresh_from_db()
            registry.record(row, state, provider_id)
        return JsonResponse({"code": state})
    except (ValueError, ValidationError):
        return JsonResponse({"code": "eligibility_unverified"})
