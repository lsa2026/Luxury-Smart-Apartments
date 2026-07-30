"""Server-to-server endpoints for integrations."""

import base64
import json
from binascii import Error as Base64Error

from django.conf import settings
from django.core.cache import cache
from django.db import DatabaseError, IntegrityError
from django.http import HttpRequest, JsonResponse
from django.utils.crypto import constant_time_compare
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from .hostaway.webhooks import sanitize_webhook_payload, webhook_rate_key
from .models import HostawayWebhookEvent


@csrf_exempt
@require_POST
def hostaway_unified_webhook(request: HttpRequest) -> JsonResponse:
    """Authenticate, sanitize, enqueue, and return without calling Hostaway."""
    if not settings.HOSTAWAY_WEBHOOK_RECEIVER_ENABLED:
        return JsonResponse({"status": "not_found"}, status=404)
    if not _basic_auth_valid(request):
        response = JsonResponse({"status": "unauthorized"}, status=401)
        response["WWW-Authenticate"] = 'Basic realm="Hostaway Webhook"'
        return response
    if _rate_limited(request):
        return JsonResponse({"status": "temporarily_unavailable"}, status=503)
    content_type = request.headers.get("Content-Type", "").split(";", 1)[0].strip().casefold()
    if content_type != "application/json":
        return JsonResponse({"status": "invalid_request"}, status=400)
    declared_length = request.META.get("CONTENT_LENGTH", "")
    try:
        if declared_length and int(declared_length) > settings.HOSTAWAY_WEBHOOK_MAX_BODY_BYTES:
            return JsonResponse({"status": "invalid_request"}, status=400)
    except ValueError:
        return JsonResponse({"status": "invalid_request"}, status=400)
    raw_body = request.body
    if len(raw_body) > settings.HOSTAWAY_WEBHOOK_MAX_BODY_BYTES:
        return JsonResponse({"status": "invalid_request"}, status=400)
    try:
        payload = json.loads(raw_body)
        sanitized = sanitize_webhook_payload(payload, raw_body)
    except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
        return JsonResponse({"status": "invalid_request"}, status=400)

    supported = sanitized.event_type in settings.HOSTAWAY_WEBHOOK_ALLOWED_EVENTS
    defaults = {
        "external_event_id": sanitized.external_event_id,
        "event_type": sanitized.event_type,
        "hostaway_object_id": sanitized.object_id,
        "hostaway_reservation_id": sanitized.reservation_id,
        "body_hash": sanitized.body_hash,
        "sanitized_payload": sanitized.payload,
        "status": (
            HostawayWebhookEvent.Status.RECEIVED
            if supported
            else HostawayWebhookEvent.Status.IGNORED
        ),
        "error_code": "" if supported else "unsupported_event",
    }
    try:
        _event, created = HostawayWebhookEvent.objects.get_or_create(
            deduplication_key=sanitized.deduplication_key,
            defaults=defaults,
        )
    except IntegrityError:
        HostawayWebhookEvent.objects.get(deduplication_key=sanitized.deduplication_key)
        created = False
    except DatabaseError:
        return JsonResponse({"status": "temporarily_unavailable"}, status=503)
    if not created:
        return JsonResponse({"status": "duplicate"}, status=200)
    return JsonResponse(
        {"status": "accepted" if supported else "ignored"},
        status=202 if supported else 200,
    )


def _basic_auth_valid(request: HttpRequest) -> bool:
    expected_username = settings.HOSTAWAY_WEBHOOK_BASIC_AUTH_USERNAME
    expected_password = settings.HOSTAWAY_WEBHOOK_BASIC_AUTH_PASSWORD
    if not expected_username or not expected_password:
        return False
    header = request.headers.get("Authorization", "")
    if not header.startswith("Basic "):
        return False
    try:
        decoded = base64.b64decode(header[6:], validate=True).decode("utf-8")
    except (Base64Error, UnicodeDecodeError):
        return False
    username, separator, password = decoded.partition(":")
    return bool(
        separator
        and constant_time_compare(username, expected_username)
        and constant_time_compare(password, expected_password)
    )


def _rate_limited(request: HttpRequest) -> bool:
    key = webhook_rate_key(request.META.get("REMOTE_ADDR", ""))
    if cache.add(key, 1, timeout=settings.HOSTAWAY_WEBHOOK_RATE_LIMIT_WINDOW):
        return False
    try:
        count = cache.incr(key)
    except ValueError:
        cache.set(key, 1, timeout=settings.HOSTAWAY_WEBHOOK_RATE_LIMIT_WINDOW)
        count = 1
    return count > settings.HOSTAWAY_WEBHOOK_RATE_LIMIT_REQUESTS
