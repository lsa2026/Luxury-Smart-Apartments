"""Durable, future-only MorningStay events verified by the website before n8n dispatch."""

import hashlib
import json
from datetime import timedelta
from zoneinfo import ZoneInfo

import httpx
from django.conf import settings
from django.core import signing
from django.db import transaction
from django.http import Http404, JsonResponse
from django.utils import timezone
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET

from apps.core.phone_numbers import InvalidPhoneNumber, normalize_phone_number

from .models import MorningCommunicationEvent, MorningStay

SALT = "reservations.morning07-communication-event.v1"


def verified_payment(stay):
    return (
        bool(stay.paid_at)
        and stay.attempts.filter(
            status="succeeded",
            amount=stay.amount_sar,
            currency="SAR",
            verified_at__isnull=False,
            provider_payment_id__isnull=False,
        )
        .exclude(provider_payment_id="")
        .exists()
    )


def capture_event(stay_id):
    """Called inside save's transaction. Open/processing/review never confirm admission."""
    stay = MorningStay.objects.select_for_update().select_related("property").get(pk=stay_id)
    if stay.environment != "production" or stay.status not in {"paid", "cancelled"}:
        return
    if stay.status == "paid" and stay.ends_at <= timezone.now():
        return
    if not verified_payment(stay) or (
        stay.status == "cancelled" and not stay.communication_revision
    ):
        return
    try:
        phone = normalize_phone_number(stay.guest_phone)
    except InvalidPhoneNumber:
        return
    zone = ZoneInfo(stay.time_zone)
    start, end = stay.starts_at.astimezone(zone), stay.ends_at.astimezone(zone)
    values = {
        "source": "morning07",
        "morning_stay_id": str(stay.pk),
        "reservation_key": "morning07:" + str(stay.pk),
        "status": stay.status,
        "payment_verified": True,
        "listing_map_id": stay.property.hostaway_listing_map_id,
        "listing_name": stay.property.name_ar or stay.property.name_en,
        "starts_at": stay.starts_at.isoformat(),
        "ends_at": stay.ends_at.isoformat(),
        "timezone": stay.time_zone,
        "arrival_date": start.date().isoformat(),
        "departure_date": end.date().isoformat(),
        "check_in_time": start.strftime("%H:%M"),
        "check_out_time": end.strftime("%H:%M"),
        "guest_name": stay.guest_name,
        "phone": phone,
        "guest_email": stay.guest_email,
        "guest_language": stay.guest_language,
        "guests": stay.guests,
        "total_price": str(stay.amount_sar),
        "currency": "SAR",
        "paid_at": stay.paid_at.isoformat(),
        "management_url": settings.SITE_BASE_URL.rstrip("/")
        + "/admin/operations/morning-stays/"
        + str(stay.pk)
        + "/",
    }
    fingerprint = hashlib.sha256(json.dumps(values, sort_keys=True).encode()).hexdigest()
    if fingerprint == stay.communication_fingerprint:
        return
    revision = stay.communication_revision + 1
    kind = "cancelled" if stay.status == "cancelled" else "new" if revision == 1 else "modified"
    event = MorningCommunicationEvent(stay=stay, revision=revision)
    event.payload = {
        **values,
        "event_type": kind,
        "source_revision": revision,
        "source_event_id": str(event.pk),
        "source_updated_at": timezone.now().isoformat(),
        "state_fingerprint": fingerprint,
    }
    event.save()
    MorningCommunicationEvent.objects.filter(stay=stay, state="pending").exclude(
        pk=event.pk
    ).update(state="superseded", last_error="newer_stay_revision")
    MorningStay.objects.filter(pk=stay.pk).update(
        communication_revision=revision, communication_fingerprint=fingerprint
    )
    transaction.on_commit(lambda: queue_stay_event(str(stay.pk)))


def queue_stay_event(stay_id):
    """Dispatch only this committed booking; no periodic poller."""
    from .tasks import deliver_morning07_events

    try:
        deliver_morning07_events.delay(stay_id)
    except Exception:
        # The durable outbox remains pending and can be retried by operations.
        import logging

        logging.getLogger(__name__).exception("Morning07 booking dispatch could not be queued")


def event_token(event):
    # A capability for ONE outbox event, not a reusable provider credential.
    return signing.TimestampSigner(salt=SALT).sign(str(event.pk))


@require_GET
@never_cache
def read_event(request, event_id):
    try:
        actual = signing.TimestampSigner(salt=SALT).unsign(
            request.headers.get("X-LSA-Morning07-Token", ""), max_age=15 * 86400
        )
    except signing.BadSignature as exc:
        raise Http404 from exc
    if actual != str(event_id) or not settings.MORNING07_N8N_ENABLED:
        raise Http404
    event = MorningCommunicationEvent.objects.select_related("stay").filter(pk=event_id).first()
    if not event:
        raise Http404
    stay = event.stay
    if (
        stay.status not in {"paid", "cancelled"}
        or not verified_payment(stay)
        or (stay.status == "paid" and stay.ends_at <= timezone.now())
    ):
        return JsonResponse({"error": "admission_not_confirmed"}, status=409)
    # An old delivery can only retrieve the latest authoritative version.
    current = stay.communication_events.get(revision=stay.communication_revision)
    return JsonResponse({**current.payload, "delivery_token": delivery_token(stay)})


def deliver_pending_events(limit=3, *, client=None, stay_id=None):
    if not settings.MORNING07_N8N_ENABLED or not settings.MORNING07_N8N_WEBHOOK_URL:
        return {"accepted": 0}
    accepted = 0
    for _ in range(limit):
        with transaction.atomic():
            event = (
                MorningCommunicationEvent.objects.select_for_update(skip_locked=True)
                .filter(
                    state="pending",
                    next_attempt_at__lte=timezone.now(),
                    **({"stay_id": stay_id} if stay_id else {}),
                )
                .exclude(lease_until__gt=timezone.now())
                .order_by("created_at")
                .first()
            )
            if not event:
                break
            if event.stay.status == "paid" and event.stay.ends_at <= timezone.now():
                event.state = "expired"
                event.last_error = "morning_stay_ended"
                event.save(update_fields=["state", "last_error"])
                continue
            event.attempts += 1
            event.lease_until = timezone.now() + timedelta(seconds=45)
            event.next_attempt_at = timezone.now() + timedelta(
                seconds=min(300, 30 * event.attempts)
            )
            event.save(update_fields=["attempts", "lease_until", "next_attempt_at"])
        # No guest fields are trusted from this POST. n8n reads them back from read_event.
        try:
            if client is None:
                with httpx.Client(
                    timeout=httpx.Timeout(15, connect=5), follow_redirects=False
                ) as remote:
                    response = remote.post(
                        settings.MORNING07_N8N_WEBHOOK_URL,
                        json={
                            "event_id": str(event.pk),
                            "event_token": event_token(event),
                        },
                    )
            else:
                response = client.post(
                    settings.MORNING07_N8N_WEBHOOK_URL,
                    json={
                        "event_id": str(event.pk),
                        "event_token": event_token(event),
                    },
                )
            result = response.json() if response.status_code in {200, 202} else {}
            if not isinstance(result, dict):
                result = {}
            ok = result.get("accepted") is True and result.get("event_id") == str(event.pk)
            error = "" if ok else "n8n_persistence_not_acknowledged"
        except (httpx.HTTPError, ValueError):
            ok, error = False, "n8n_transport_uncertain"
        updates = {"lease_until": None, "last_error": error}
        if ok:
            updates.update(state="accepted", accepted_at=timezone.now())
            accepted += 1
        MorningCommunicationEvent.objects.filter(pk=event.pk, state="pending").update(**updates)
    return {"accepted": accepted}


def delivery_token(stay):
    return signing.TimestampSigner(salt="reservations.morning07-delivery.v1").sign(
        f"{stay.pk}:{stay.communication_revision}"
    )
