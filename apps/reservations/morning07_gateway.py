"""Scenario 07 provider gateway: credentials stay on the website server."""

import hashlib
import json
import re

import httpx
from django.conf import settings
from django.core import signing
from django.db import transaction
from django.http import Http404, JsonResponse
from django.utils import timezone
from django.views.decorators.cache import never_cache
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_POST

from apps.notifications.services.ultramsg import (
    UltraMsgClient,
    UltraMsgConfigurationError,
    UltraMsgConnectionError,
    UltraMsgResponseError,
)

from .models import Morning07DeliveryAttempt, MorningStay
from .morning07_communications import verified_payment

KINDS = {
    "status_new",
    "status_modified",
    "status_cancelled",
    "house_rules",
    "checkin_guide",
    "reminder_checkout",
    "group_notice",
    "attention",
}


def authorize(request, *, latest=True):
    if not settings.MORNING07_N8N_ENABLED:
        raise Http404
    try:
        value = signing.TimestampSigner(salt="reservations.morning07-delivery.v1").unsign(
            request.headers.get("X-LSA-Morning07-Token", ""), max_age=15 * 86400
        )
        identifier, revision = value.rsplit(":", 1)
        stay = MorningStay.objects.get(pk=identifier)
        revision = int(revision)
    except (signing.BadSignature, ValueError, MorningStay.DoesNotExist) as exc:
        raise Http404 from exc
    if (
        stay.environment != "production"
        or stay.status not in {"paid", "cancelled"}
        or not verified_payment(stay)
    ):
        raise Http404
    if latest and revision != stay.communication_revision:
        raise Http404
    return stay, revision


@csrf_exempt
@require_POST
@never_cache
def send_message(request):
    stay, revision = authorize(request)
    try:
        data = json.loads(request.body)
        reference = data["reference_id"]
        key, kind = data["job_key"], data["message_type"]
        body, recipient_kind = data["message_body"], data["recipient_kind"]
        expected_reference = "lsa07-" + hashlib.md5(key.encode(), usedforsecurity=False).hexdigest()
        expected_recipient = (
            settings.MORNING07_WORK_GROUP_ID
            if recipient_kind == "group"
            else re.sub(r"\D", "", stay.guest_phone)
        )
        if (
            not isinstance(body, str)
            or not 1 <= len(body) <= 4096
            or kind not in KINDS
            or recipient_kind not in {"group", "guest"}
            or reference != expected_reference
            or not key.startswith(f"lsa07:{stay.pk}:")
            or data["recipient_phone"] != expected_recipient
            or ((kind in {"group_notice", "attention"}) != (recipient_kind == "group"))
            or (
                kind
                in {
                    "status_new",
                    "status_modified",
                    "house_rules",
                    "checkin_guide",
                    "reminder_checkout",
                }
                and stay.ends_at <= timezone.now()
            )
            or (
                stay.status == "cancelled"
                and kind not in {"status_cancelled", "group_notice", "attention"}
            )
        ):
            raise ValueError
    except (KeyError, TypeError, ValueError, AttributeError):
        return JsonResponse({"sent": False, "error": "invalid_morning07_job"}, status=400)
    fingerprint = hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()
    with transaction.atomic():
        attempt, created = Morning07DeliveryAttempt.objects.select_for_update().get_or_create(
            reference_id=reference,
            defaults={"stay": stay, "revision": revision, "payload_hash": fingerprint},
        )
        if attempt.stay_id != stay.pk or attempt.payload_hash != fingerprint:
            return JsonResponse({"sent": False, "error": "reference_payload_conflict"}, status=409)
        if not created:
            # A timeout may follow a provider write. Reconcile, never blindly repeat the POST.
            return JsonResponse(
                attempt.response or {"sent": "unknown", "id": attempt.provider_id}, status=202
            )
    try:
        with UltraMsgClient() as provider:
            result = provider.send_text(
                recipient=expected_recipient, body=body, reference_id=reference
            )
        result = {"sent": "true", "id": str(result.get("id") or ""), "referenceId": reference}
        state = "accepted"
    except UltraMsgConfigurationError:
        result, state = {"sent": False, "error": "provider_disabled"}, "rejected"
    except UltraMsgResponseError:
        result, state = {"sent": False, "error": "provider_rejected"}, "rejected"
    except UltraMsgConnectionError:
        result, state = {"sent": "unknown", "error": "provider_result_uncertain"}, "uncertain"
    Morning07DeliveryAttempt.objects.filter(pk=reference).update(
        response=result, state=state, provider_id=result.get("id", "")
    )
    return JsonResponse(result, status=202)


def provider_rows(document):
    if isinstance(document, list):
        return [x for x in document if isinstance(x, dict)]
    if isinstance(document, dict):
        for field in ("messages", "data", "result"):
            if isinstance(document.get(field), list | dict):
                return provider_rows(document[field])
        return [document]
    return []


@require_GET
@never_cache
def receipt(request):
    stay, _revision = authorize(request, latest=False)
    reference = request.GET.get("reference_id", "")
    attempt = Morning07DeliveryAttempt.objects.filter(pk=reference, stay=stay).first()
    if not attempt:
        raise Http404
    if attempt.state == "rejected":
        return JsonResponse({"messages": [], "error": attempt.response.get("error")})
    try:
        with UltraMsgClient() as provider:
            response = provider.http.get(
                f"/{settings.ULTRAMSG_INSTANCE_ID}/messages",
                params={
                    "token": settings.ULTRAMSG_TOKEN,
                    "id": attempt.provider_id,
                    "referenceId": "" if attempt.provider_id else reference,
                    "status": "all",
                    "limit": 100,
                    "page": 1,
                    "sort": "desc",
                },
            )
            response.raise_for_status()
            rows = provider_rows(response.json())
    except (httpx.HTTPError, ValueError, UltraMsgConfigurationError):
        return JsonResponse({"messages": [], "error": "receipt_lookup_uncertain"}, status=502)
    exact = []
    for row in rows:
        row_id = str(row.get("id") or row.get("message_id") or "")
        row_reference = str(row.get("referenceId") or row.get("reference_id") or "")
        if (attempt.provider_id and row_id == attempt.provider_id) or row_reference == reference:
            exact.append(
                {
                    "id": row_id,
                    "referenceId": row_reference,
                    "ack": row.get("ack"),
                    "status": row.get("status"),
                }
            )
    return JsonResponse({"messages": exact})
