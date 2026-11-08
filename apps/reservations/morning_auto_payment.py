"""Automatic payment-link delivery only after the three morning guest receipts."""

import json
import uuid

from django.conf import settings
from django.db import transaction
from django.http import JsonResponse
from django.utils import timezone
from django.views.decorators.cache import never_cache
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from apps.properties.models import Property

from . import morning_payment_messages as links
from .models import MorningPaymentLinkDelivery, MorningStay
from .morning07_gateway import authorize
from .morning_hostaway import booking_state, hostaway_first
from .morning_loyalty import core_delivered
from .morning_stays import MorningStayError

CLAIMED = ("queued", "sending", "accepted", "uncertain", "delivered", "read")


def eligible(stay, revision):
    if (
        not settings.MORNING07_N8N_ENABLED
        or stay.environment != "production"
        or not hostaway_first(stay)
        or booking_state(stay).get("state") != "confirmed"
        or stay.communication_revision != revision
        or stay.ends_at <= timezone.now()
    ):
        return False
    try:
        links.require_unpaid(stay)
    except MorningStayError:
        return False
    return True


def dispatch(delivery_id, revision):
    from .tasks import complete_morning_payment_link

    try:
        complete_morning_payment_link.delay(str(delivery_id), revision)
    except Exception:
        MorningPaymentLinkDelivery.objects.filter(pk=delivery_id, state="waiting_core").update(
            state="failed", last_error="queue_unavailable"
        )


@csrf_exempt
@require_POST
@never_cache
def after_core(request):
    stay, revision = authorize(request)
    try:
        if len(request.body) > 1024:
            raise ValueError
        body = json.loads(request.body)
        if (
            not isinstance(body, dict)
            or set(body) != {"core_delivered"}
            or type(body["core_delivered"]) is not bool
        ):
            raise ValueError
    except (ValueError, TypeError):
        return JsonResponse({"code": "invalid_request"}, status=400)
    if not body["core_delivered"]:
        return JsonResponse({"code": "core_pending"})
    with transaction.atomic():
        Property.objects.select_for_update().get(pk=stay.property_id)
        stay = MorningStay.objects.select_for_update().get(pk=stay.pk)
        if not eligible(stay, revision):
            return JsonResponse({"code": "closed"})
        existing = stay.payment_link_deliveries.filter(state__in=CLAIMED).first()
        if existing:
            return JsonResponse({"code": existing.state})
        delivery, created = MorningPaymentLinkDelivery.objects.get_or_create(
            pk=uuid.uuid5(stay.pk, "initial-payment-link"),
            defaults={
                "stay": stay,
                "state": "waiting_core",
                "fingerprint": links.fingerprint(stay),
            },
        )
        if created:
            transaction.on_commit(lambda: dispatch(delivery.pk, revision))
        return JsonResponse({"code": delivery.state}, status=202)


def complete(delivery_id, revision):
    """Return core_pending for a bounded retry; a provider POST is never blindly repeated."""
    delivery = MorningPaymentLinkDelivery.objects.select_related("stay__property").get(
        pk=delivery_id
    )
    if delivery.state not in {"waiting_core", "queued"}:
        return delivery.state
    stay = delivery.stay
    if not eligible(stay, revision) or delivery.fingerprint != links.fingerprint(stay):
        MorningPaymentLinkDelivery.objects.filter(
            pk=delivery.pk, state__in=["waiting_core", "queued"]
        ).update(state="expired", last_error="eligibility_changed")
        return "expired"
    if delivery.state == "queued":
        return links.send_link(delivery.pk)
    try:
        if not core_delivered(stay, revision):
            return "core_pending"
    except ValueError:
        return "core_pending"
    with transaction.atomic():
        Property.objects.select_for_update().get(pk=stay.property_id)
        stay = MorningStay.objects.select_for_update().get(pk=stay.pk)
        delivery = MorningPaymentLinkDelivery.objects.select_for_update().get(pk=delivery.pk)
        if delivery.state != "waiting_core":
            return delivery.state
        if (
            not eligible(stay, revision)
            or delivery.fingerprint != links.fingerprint(stay)
            or stay.payment_link_deliveries.exclude(pk=delivery.pk)
            .filter(state__in=CLAIMED)
            .exists()
        ):
            delivery.state, delivery.last_error = "expired", "eligibility_changed"
            delivery.save(update_fields=["state", "last_error"])
            return "expired"
        delivery.state = "queued"
        delivery.save(update_fields=["state"])
    return links.send_link(delivery.pk)
