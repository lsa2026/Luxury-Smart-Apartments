"""Private extension capability and durable transport; no provider messages here."""

from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

import httpx
from django.core import signing
from django.db import transaction
from django.http import Http404, JsonResponse
from django.utils import timezone
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET

from .models import StayExtension, StayExtensionEvent
from .stay_extensions import (
    booking_verified,
    chain_context,
    guest_snapshot,
    payment_verified,
    read_source,
    source_snapshot,
)

SALT = "reservations.stay-extension.context.v1"
WEBHOOK = "https://lsa25.app.n8n.cloud/webhook/lsa08-stay-extension-v1"
HEALTH = "https://lsa25.app.n8n.cloud/webhook/lsa-stay-extension-health-v1"


def expires_at(extension):
    state = chain_context(extension.chain)
    zone = ZoneInfo(state["timezone"])
    departure = datetime.fromisoformat(state["effective_checkout_at"]).date()
    # Longest existing review window is covered without a universal credential.
    return datetime.combine(
        max(extension.draft.check_out, departure) + timedelta(days=90), time.max, zone
    )


def capability(extension, *, event=None):
    return signing.dumps(
        {
            "extension_id": str(extension.pk),
            "expires": int(expires_at(extension).timestamp()),
            "event_id": str(event.pk) if event else None,
        },
        salt=SALT,
    )


def _envelope(extension, *, phase, event=None):
    return {
        "source": "stay_extension",
        "contract_version": 1,
        "extension_id": str(extension.pk),
        "event_token": capability(extension, event=event),
        "phase": phase,
        "event_id": str(event.pk) if event else None,
        "revision": event.revision if event else extension.chain.revision,
        "source_revision": event.revision if event else extension.chain.revision,
        "dry_run": False,
    }


def receiver_ready(*, transport=None):
    def check(client):
        response = client.get(HEALTH)
        response.raise_for_status()
        body = response.json()
        return (
            body.get("contract_version") == 1
            and body.get("build_ready") is True
            and body.get("receive_enabled") is True
        )

    try:
        if transport is not None:
            return check(transport)
        with httpx.Client(timeout=15, follow_redirects=False) as client:
            return check(client)
    except Exception:
        return False


def acknowledge_ownership(extension_id, *, transport=None):
    if not receiver_ready(transport=transport):
        return False
    extension = StayExtension.objects.select_related("chain__property", "draft").get(
        pk=extension_id
    )
    if extension.ownership_acknowledged:
        return True
    revision = extension.chain.revision

    def post(client):
        response = client.post(WEBHOOK, json=_envelope(extension, phase="ownership"))
        response.raise_for_status()
        body = response.json()
        return (
            body.get("code") == "extension_ownership_persisted"
            and body.get("extension_id") == str(extension.pk)
            and type(body.get("revision")) is int
            and body["revision"] == revision
            and body.get("contract_version") == 1
        )

    try:
        if transport is not None:
            accepted = post(transport)
        else:
            with httpx.Client(timeout=20, follow_redirects=False) as client:
                accepted = post(client)
    except Exception:
        accepted = False
    if accepted:
        with transaction.atomic():
            extension = (
                StayExtension.objects.select_for_update()
                .select_related("chain")
                .get(pk=extension_id)
            )
            if extension.chain.revision != revision:
                return False
            extension.ownership_acknowledged = True
            extension.save(update_fields=["ownership_acknowledged", "updated_at"])
        return True
    return False


def context(extension, *, verify_live=True, client=None):
    from .services.stay_policy import stay_policy_for

    chain = extension.chain
    state = chain_context(chain)
    policy = stay_policy_for(chain.property)
    ready = state["chain_state"] != "review"
    if verify_live:
        # A stale/dropped webhook cannot authorize sending against changed dates.
        # No writes, messages, or collection checks are performed by this GET.
        try:
            root = source_snapshot(read_source(chain.root_hostaway_reservation_id, client=client))
            if root != chain.root_snapshot:
                ready = False
            for item in chain.extensions.select_related("reservation", "draft"):
                reservation = item.reservation
                if reservation and reservation.hostaway_reservation_id:
                    raw = read_source(reservation.hostaway_reservation_id, client=client)
                    remote = source_snapshot(raw)
                    if (
                        remote["listing_map_id"] != chain.property.hostaway_listing_map_id
                        or remote["arrival_date"] != reservation.check_in.isoformat()
                        or remote["departure_date"] != reservation.check_out.isoformat()
                        or remote["status"] != reservation.normalized_status
                        or remote["channel_id"] != reservation.channel_id
                        or str(raw.get("currency", "")).upper() != reservation.currency
                    ):
                        ready = False
                    from decimal import Decimal

                    if Decimal(str(raw.get("totalPrice"))) != reservation.total_price:
                        ready = False
        except Exception:
            ready = False
    if not ready:
        state["chain_state"] = "review"
        state["active_linkage_verified"] = False
        for member in state["chain_members"]:
            member.update(allow_checkout=False, review_allowed=False, review_anchor_date=None)
    reservation = extension.reservation
    return {
        "source": "stay_extension",
        "contract_version": 1,
        "extension_id": str(extension.pk),
        "revision": chain.revision,
        "state_fingerprint": chain.fingerprint,
        "state": extension.state,
        "root_hostaway_reservation_id": chain.root_hostaway_reservation_id,
        "previous_hostaway_reservation_id": extension.previous_hostaway_reservation_id,
        "new_hostaway_reservation_id": reservation.hostaway_reservation_id if reservation else None,
        "listing_map_id": chain.property.hostaway_listing_map_id,
        "check_in": extension.draft.check_in.isoformat(),
        "check_out": extension.draft.check_out.isoformat(),
        "previous_check_out": extension.previous_snapshot["departure_date"],
        "payment_verified": payment_verified(reservation),
        "booking_verified": booking_verified(extension),
        "ownership_reference": "EXT01:" + str(extension.pk),
        "communication_ready": ready,
        "activation_generation": extension.activation_generation,
        "creation_ready": extension.draft.status == "ready_for_payment" and bool(reservation),
        "communication_snapshot": guest_snapshot(extension),
        "capability_expires_at": expires_at(extension).isoformat(),
        "check_in_time": f"{policy.check_in_hour:02}:00"
        if policy.check_in_hour is not None
        else None,
        "check_out_time": f"{policy.check_out_hour:02}:00"
        if policy.check_out_hour is not None
        else None,
        **state,
    }


@require_GET
@never_cache
def read_context(request, extension_id):
    token = request.headers.get("X-LSA-Extension-Token", "")
    try:
        claims = signing.loads(token, salt=SALT, max_age=500 * 86400)
        if (
            claims["extension_id"] != str(extension_id)
            or type(claims["expires"]) is not int
            or claims["expires"] <= timezone.now().timestamp()
        ):
            raise ValueError
        extension = StayExtension.objects.select_related(
            "chain__property", "draft", "reservation__booking_intent"
        ).get(pk=extension_id)
        event = None
        if claims.get("event_id"):
            event = StayExtensionEvent.objects.get(pk=claims["event_id"], extension=extension)
    except (
        signing.BadSignature,
        ValueError,
        KeyError,
        TypeError,
        StayExtension.DoesNotExist,
        StayExtensionEvent.DoesNotExist,
    ):
        raise Http404 from None
    values = context(extension)
    values.update(
        capability_purpose="event" if event else "ownership",
        source_event_id=str(event.pk) if event else None,
        source_event_revision=event.revision if event else None,
        source_event_kind=event.kind if event else None,
    )
    response = JsonResponse(values)
    response["Referrer-Policy"] = "no-referrer"
    return response


@require_GET
@never_cache
def health(request):
    # Public readiness metadata contains no guest or private operational data.
    return JsonResponse({"source": "stay_extension", "contract_version": 1, "build_ready": True})


def deliver_events(extension_id, *, transport=None):
    accepted = 0
    events = StayExtensionEvent.objects.filter(extension_id=extension_id, state="pending").order_by(
        "revision"
    )
    for candidate in events[:25]:
        with transaction.atomic():
            event = StayExtensionEvent.objects.select_for_update().get(pk=candidate.pk)
            if event.state != "pending" or (
                event.lease_until and event.lease_until > timezone.now()
            ):
                continue
            if event.attempts >= 10:
                event.state = "review"
                event.save(update_fields=["state"])
                continue
            extension = StayExtension.objects.select_related("chain__property", "draft").get(
                pk=extension_id
            )
            if event.revision < extension.chain.revision:
                event.state = "superseded"
                event.save(update_fields=["state"])
                continue
            event.attempts += 1
            event.lease_until = timezone.now() + timedelta(seconds=45)
            event.save(update_fields=["attempts", "lease_until"])

        def post(client, extension=extension, event=event):
            response = client.post(WEBHOOK, json=_envelope(extension, phase="event", event=event))
            response.raise_for_status()
            body = response.json()
            valid = (
                body.get("accepted") is True
                and body.get("event_id") == str(event.pk)
                and body.get("extension_id") == str(extension.pk)
                and type(body.get("revision")) is int
                and body["revision"] == event.revision
                and body.get("contract_version") == 1
                and type(body.get("delivery_pending")) is bool
            )
            return valid, body.get("delivery_pending", True)

        try:
            if transport is not None:
                acknowledged, delivery_pending = post(transport)
            else:
                with httpx.Client(timeout=20, follow_redirects=False) as client:
                    acknowledged, delivery_pending = post(client)
        except Exception:
            acknowledged, delivery_pending = False, True
        with transaction.atomic():
            locked = StayExtensionEvent.objects.select_for_update().get(pk=event.pk)
            locked.lease_until = None
            if acknowledged:
                locked.acknowledged_at = timezone.now()
                if not delivery_pending:
                    locked.state = "accepted"
                    accepted += 1
            locked.save(update_fields=["state", "acknowledged_at", "lease_until"])
    return {"accepted": accepted}
