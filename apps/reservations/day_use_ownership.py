"""Authoritative, revision-scoped ownership registered before a provider write."""

import httpx
from django.conf import settings
from django.core import signing
from django.db import transaction
from django.http import Http404, JsonResponse
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET

from .models import DayUseHostawayOwnership

SALT = "reservations.day-use08.ownership.v1"


def capability(row):
    return signing.TimestampSigner(salt=SALT).sign(f"{row.stay_id}:{row.revision}")


@require_GET
@never_cache
def read_ownership(request, stay_id):
    if not settings.DAY_USE08_ENABLED:
        raise Http404
    try:
        value = signing.TimestampSigner(salt=SALT).unsign(
            request.headers.get("X-LSA-DayUse08-Token", ""), max_age=900
        )
        identifier, revision = value.rsplit(":", 1)
        if identifier != str(stay_id):
            raise ValueError
        row = DayUseHostawayOwnership.objects.select_related("stay__property").get(
            stay_id=stay_id, revision=int(revision), stay__environment="production"
        )
    except (signing.BadSignature, ValueError, DayUseHostawayOwnership.DoesNotExist) as exc:
        raise Http404 from exc
    stay = row.stay
    return JsonResponse(
        {
            "source": "day_use08_ownership",
            "contract_version": 1,
            "day_use_stay_id": str(stay.pk),
            "request_id": str(stay.request_id),
            "ownership_revision": row.revision,
            "state": row.state,
            "ownership_reference": f"DU08:{stay.pk}",
            "listing_map_id": stay.property.hostaway_listing_map_id,
            "starts_at": stay.starts_at.isoformat(),
            "ends_at": stay.ends_at.isoformat(),
            "timezone": stay.time_zone,
            "inventory_arrival_date": stay.inventory_arrival_date.isoformat(),
            "inventory_departure_date": stay.inventory_departure_date.isoformat(),
            "total_price": str(stay.amount_sar),
            "currency": "SAR",
            "hostaway_reservation_id": row.provider_reservation_id,
        }
    )


def sync(stay_id, *, client=None):
    """An HTTP success alone is insufficient: exact persistence ACK is required."""
    if not settings.DAY_USE08_ENABLED or not settings.DAY_USE08_OWNERSHIP_WEBHOOK_URL:
        return False
    row = DayUseHostawayOwnership.objects.get(stay_id=stay_id)
    revision = row.revision
    if row.acknowledged_revision == revision:
        return True
    envelope = {"day_use_stay_id": str(row.stay_id), "event_token": capability(row)}
    try:
        if client is None:
            with httpx.Client(
                timeout=httpx.Timeout(15, connect=5), follow_redirects=False
            ) as remote:
                return sync(stay_id, client=remote)
        response = client.post(settings.DAY_USE08_OWNERSHIP_WEBHOOK_URL, json=envelope)
        body = response.json() if response.status_code in {200, 202} else {}
        ok = (
            isinstance(body, dict)
            and body.get("code") == "ownership_persisted"
            and body.get("day_use_stay_id") == str(row.stay_id)
            and type(body.get("ownership_revision")) is int
            and body["ownership_revision"] == revision
        )
    except (httpx.HTTPError, ValueError):
        ok = False
    updates = {"last_error": "" if ok else "ownership_not_acknowledged"}
    if ok:
        updates["acknowledged_revision"] = revision
    changed = DayUseHostawayOwnership.objects.filter(stay_id=stay_id, revision=revision).update(
        **updates
    )
    return bool(ok and changed)


def update(stay_id, state, reservation_id=None):
    with transaction.atomic():
        row = DayUseHostawayOwnership.objects.select_for_update().get(stay_id=stay_id)
        if row.state == state and row.provider_reservation_id == reservation_id:
            return row
        row.state, row.provider_reservation_id = state, reservation_id
        row.revision += 1
        row.save(update_fields=["state", "provider_reservation_id", "revision", "updated_at"])
        transaction.on_commit(lambda: queue(str(stay_id)), robust=True)
        return row


def queue(stay_id):
    from .tasks import sync_day_use_ownership

    sync_day_use_ownership.delay(stay_id)
