"""Independent direct extensions with fail-closed, contiguous-stay decisions."""

import hashlib
import json
import logging
import re
from datetime import UTC, date, timedelta
from types import SimpleNamespace

from django.conf import settings
from django.db import transaction
from django.utils import timezone, translation

from apps.integrations.hostaway.client import HostawayClient
from apps.integrations.hostaway.reservation_validators import (
    normalize_hostaway_reservation_status,
    validate_reservation_response,
)
from apps.payments.models import PaymentAttempt
from apps.properties.models import Property

from .models import Reservation, StayExtension, StayExtensionChain, StayExtensionEvent
from .services.stay_lifecycle import checkout_datetime

logger = logging.getLogger(__name__)


class UnsupportedStaySource(ValueError):
    pass


def unsupported_source_reason(raw):
    # Same metadata classifier as the published normal automation. No channel
    # number is guessed; a missing separate registry entry cannot clear a match.
    names = (
        "guestEmail",
        "guest_email",
        "source",
        "customerIcalName",
        "manualIcalName",
        "channelName",
        "booking_platform",
    )
    if any(
        re.search(r"gathern(?:[.]co)?|gatherin|غاثرن|قاثرن|جاذر", str(raw.get(name) or ""), re.I)
        for name in names
    ):
        return "تمديد غاثرن غير متاح مؤقتًا؛ مسار رسائله المستقل يحتاج ربطًا بالسلسلة."
    return ""


def _checkout(property_obj, departure):
    value = checkout_datetime(SimpleNamespace(property=property_obj, check_out=departure))
    zone_name = str(value.tzinfo)
    # IANA tzdb2026c: Morocco switched permanently to UTC on 2026-09-20.
    # Match the existing automation compatibility rule if host tzdata is old.
    if zone_name == "Africa/Casablanca" and (
        departure > date(2026, 9, 20) or (departure == date(2026, 9, 20) and value.hour >= 2)
    ):
        value = value.replace(tzinfo=UTC)
    return value, zone_name


def digest(values):
    return hashlib.sha256(json.dumps(values, sort_keys=True, default=str).encode()).hexdigest()


def source_snapshot(raw):
    """Allowlist only the identity and immutable stay coordinates, never raw JSON."""
    item = validate_reservation_response({"status": "success", "result": raw})
    return {
        "id": item.reservation_id,
        "listing_map_id": item.listing_map_id,
        "channel_id": item.channel_id,
        "channel_name": str(raw.get("channelName") or raw.get("source") or "غير محدد")[:80],
        "arrival_date": item.check_in.isoformat(),
        "departure_date": item.check_out.isoformat(),
        "status": normalize_hostaway_reservation_status(item.status),
        "guests": item.guests,
    }


def read_source(reservation_id, *, client=None):
    if client is not None:
        return client.get_reservation_invoice_data(reservation_id)["result"]
    with HostawayClient() as owned:
        return owned.get_reservation_invoice_data(reservation_id)["result"]


def eligible_source(raw, *, for_display=False):
    snapshot = source_snapshot(raw)
    unsupported = unsupported_source_reason(raw)
    if unsupported and not for_display:
        raise UnsupportedStaySource(unsupported)
    note = str(raw.get("hostNote") or "")
    provider = str(raw.get("provider") or "")
    from .models import DayUseHostawayOwnership, DayUseStay, MorningStay

    if (
        note.startswith(("DU08:", "MorningStay "))
        or provider in {"LSA_DayUse", "LSA_MorningStay"}
        or DayUseHostawayOwnership.objects.filter(provider_reservation_id=snapshot["id"]).exists()
        or DayUseStay.objects.filter(
            availability_evidence__hostaway_day_use__reservation_id=snapshot["id"]
        ).exists()
        or MorningStay.objects.filter(
            availability_evidence__hostaway_morning__reservation_id=snapshot["id"]
        ).exists()
    ):
        raise ValueError("special_stay_has_own_journey")
    property_obj = Property.objects.filter(
        hostaway_listing_map_id=snapshot["listing_map_id"],
        hostaway_is_active=True,
        is_visible=True,
    ).first()
    if property_obj is None or snapshot["status"] not in Reservation.ACTIVE_STATUSES:
        raise ValueError("source_not_active")
    zone = _checkout(property_obj, date.fromisoformat(snapshot["departure_date"]))[0].tzinfo
    today = timezone.now().astimezone(zone).date()
    if not (
        date.fromisoformat(snapshot["arrival_date"])
        <= today
        <= date.fromisoformat(snapshot["departure_date"])
    ):
        raise ValueError("source_not_current")
    return snapshot, property_obj


def guest_initial(raw):
    # Corrections are written to the new direct draft only, never the OTA record.
    name = str(raw.get("guestName") or "").strip().split(maxsplit=1)
    language = str(raw.get("guestLocale") or "ar").split("-", 1)[0].casefold()
    return {
        "guest_first_name": str(raw.get("guestFirstName") or (name[0] if name else ""))[:100],
        "guest_last_name": str(raw.get("guestLastName") or (name[1] if len(name) > 1 else ""))[
            :100
        ],
        "guest_email": str(raw.get("guestEmail") or "")[:254],
        "guest_phone": str(raw.get("phone") or "")[:32],
        "guest_language": language if language in {"ar", "en", "fr"} else "ar",
    }


def start_extension(*, raw, check_out, guests, actor, guest_data=None, availability_service=None):
    from .manual_bookings import create_manual_booking_draft

    snapshot, property_obj = eligible_source(raw)
    check_in = date.fromisoformat(snapshot["departure_date"])
    if check_out <= check_in or check_out > check_in + timedelta(days=365):
        raise ValueError("invalid_extension_dates")
    # The property lock is also used by provider creation. Unique parent prevents
    # two clicks, two managers, or a generic draft URL from making a second child.
    with transaction.atomic():
        Property.objects.select_for_update().get(pk=property_obj.pk)
        existing = (
            StayExtension.objects.filter(previous_hostaway_reservation_id=snapshot["id"])
            .exclude(state="cancelled")
            .first()
        )
        if existing:
            return existing
        prior = (
            StayExtension.objects.select_related("chain")
            .filter(reservation__hostaway_reservation_id=snapshot["id"])
            .first()
        )
        if prior:
            chain = StayExtensionChain.objects.select_for_update().get(pk=prior.chain_id)
            state = chain_context(chain)
            if (
                state["chain_state"] != "active"
                or state["terminal_hostaway_reservation_id"] != snapshot["id"]
            ):
                raise ValueError("previous_extension_not_active")
        else:
            chain, _ = StayExtensionChain.objects.get_or_create(
                root_hostaway_reservation_id=snapshot["id"],
                defaults={"property": property_obj, "root_snapshot": snapshot},
            )
            if chain.root_snapshot != snapshot or chain.review_required:
                raise ValueError("source_changed")
        if chain.extensions.count() >= 20:
            raise ValueError("chain_requires_review")
        result = create_manual_booking_draft(
            property_obj=property_obj,
            check_in=check_in,
            check_out=check_out,
            guests=guests,
            actor=actor,
            availability_service=availability_service,
        )
        if result.draft is None:
            raise ValueError(result.code)
        draft = result.draft
        values = guest_initial(raw)
        if guest_data:
            values.update({key: guest_data[key] for key in values})
        for key, value in values.items():
            setattr(draft, key, value)
        draft.save()
        extension = StayExtension.objects.create(
            chain=chain,
            previous_hostaway_reservation_id=snapshot["id"],
            previous_snapshot=snapshot,
            draft=draft,
            created_by=actor,
        )
        return extension


def payment_verified(reservation):
    if (
        not reservation
        or reservation.is_test
        or not reservation.booking_intent_id
        or reservation.payment_status != "paid"
    ):
        return False
    intent = reservation.booking_intent
    attempts = (
        PaymentAttempt.objects.filter(
            booking_intent_id=intent.pk,
            status="succeeded",
            verified_at__isnull=False,
        )
        .filter(provider_payment_id__isnull=False)
        .exclude(provider_payment_id="")
    )
    # Existing collection proofs retain their native currency and amount.
    manual = attempts.filter(
        provider="manual", amount=reservation.total_price, currency=reservation.currency
    )
    online = attempts.filter(
        provider="hyperpay",
        amount=intent.payment_amount_sar,
        currency="SAR",
        payment_request__status="paid",
        payment_request__reservation=reservation,
        payment_request__source_amount=reservation.total_price,
        payment_request__source_currency=reservation.currency,
        payment_request__environment=settings.HYPERPAY_ENVIRONMENT,
    )
    return manual.exists() or (settings.HYPERPAY_ENVIRONMENT == "production" and online.exists())


def booking_verified(extension):
    reservation = extension.reservation
    if not reservation:
        return False
    draft = extension.draft
    return bool(
        extension.ownership_acknowledged
        and reservation.hostaway_reservation_id
        and reservation.channel_id == settings.HOSTAWAY_DIRECT_CHANNEL_ID
        and reservation.normalized_status in Reservation.ACTIVE_STATUSES
        and reservation.source_type == Reservation.SourceType.DIRECT_WEBSITE
        and reservation.hostaway_listing_map_id == extension.chain.property.hostaway_listing_map_id
        and reservation.check_in == draft.check_in
        and reservation.check_out == draft.check_out
        and reservation.currency == draft.currency
        and reservation.total_price == draft.final_total_price
        and reservation.check_in.isoformat() == extension.previous_snapshot["departure_date"]
    )


def chain_context(chain):
    """Pure projection: advance only through paid, verified contiguous members."""
    root = chain.root_snapshot
    members = [
        {
            "hostaway_reservation_id": root["id"],
            "arrival_date": root["arrival_date"],
            "departure_date": root["departure_date"],
            "state": "original",
            "booking_platform": root["channel_name"],
        }
    ]
    terminal, checkout = root["id"], root["departure_date"]
    connected = {terminal}
    review = chain.review_required
    active = False
    extensions = list(
        chain.extensions.select_related("reservation__booking_intent", "draft", "chain__property")
    )
    if len(extensions) > 20:
        review = True
    for extension in extensions:
        reservation = extension.reservation
        provider_id = reservation.hostaway_reservation_id if reservation else None
        if (
            extension.state == "cancelled"
            and reservation
            and reservation.normalized_status not in Reservation.CLOSED_STATUSES
        ):
            review = True
        if provider_id:
            members.append(
                {
                    "hostaway_reservation_id": provider_id,
                    "arrival_date": reservation.check_in.isoformat(),
                    "departure_date": reservation.check_out.isoformat(),
                    "state": extension.state,
                    "booking_platform": "direct_website",
                }
            )
        if extension.state == "review":
            review = True
        if extension.state != "active":
            continue
        if (
            extension.previous_hostaway_reservation_id != terminal
            or extension.draft.check_in.isoformat() != checkout
        ):
            review = True
            continue
        if not booking_verified(extension) or not payment_verified(reservation):
            review = True
            continue
        terminal, checkout, active = provider_id, reservation.check_out.isoformat(), True
        connected.add(terminal)
    checkout_at, zone_name = _checkout(chain.property, date.fromisoformat(checkout))
    for member in members:
        in_stay = member["hostaway_reservation_id"] in connected
        member.update(
            allow_checkout=not review and in_stay and member["hostaway_reservation_id"] == terminal,
            review_allowed=not review and in_stay,
            review_anchor_date=checkout if in_stay and not review else None,
        )
    return {
        "stay_chain_id": str(chain.pk),
        "chain_revision": chain.revision,
        "chain_fingerprint": chain.fingerprint,
        "chain_state": "review" if review else ("active" if active else "original"),
        "chain_members": members,
        "terminal_hostaway_reservation_id": terminal,
        "effective_checkout_at": checkout_at.isoformat(),
        "timezone": zone_name,
        "active_linkage_verified": active and not review,
    }


def _schedule(extension_id):
    from .tasks import deliver_stay_extension_events

    deliver_stay_extension_events.delay(str(extension_id))


def refresh_chain(chain_id):
    """Called after committed booking/collection changes, never from a GET."""
    with transaction.atomic():
        chain = (
            StayExtensionChain.objects.select_for_update()
            .select_related("property")
            .get(pk=chain_id)
        )
        extensions = list(
            chain.extensions.select_related(
                "reservation__booking_intent", "draft", "chain__property"
            )
        )
        changes = []
        for extension in extensions:
            previous = extension.state
            reservation = extension.reservation
            if reservation and reservation.normalized_status in Reservation.CLOSED_STATUSES:
                state = "cancelled"
            elif reservation and reservation.hostaway_reservation_id:
                state = (
                    "active"
                    if booking_verified(extension) and payment_verified(reservation)
                    else "created_unpaid"
                )
                if not booking_verified(extension) or (previous == "active" and state != "active"):
                    state = "review"
            else:
                state = previous
            if state != previous:
                extension.state = state
                if state == "active":
                    extension.activation_generation += 1
                extension.save(update_fields=["state", "activation_generation", "updated_at"])
                changes.append((extension, state))
        context = chain_context(chain)
        # Revisions cover communication-relevant data, including corrected contact,
        # but activation_generation changes only on genuine activation transitions.
        material = {
            "chain": {
                k: v for k, v in context.items() if k not in {"chain_revision", "chain_fingerprint"}
            },
            "extensions": [
                {
                    "id": str(e.pk),
                    "state": e.state,
                    "generation": e.activation_generation,
                    "guest": guest_snapshot(e),
                    "ack": e.ownership_acknowledged,
                }
                for e in extensions
            ],
        }
        fingerprint = digest(material)
        if chain.fingerprint == fingerprint:
            return
        chain.revision += 1
        chain.fingerprint = fingerprint
        chain.save(update_fields=["revision", "fingerprint", "updated_at"])
        for extension in extensions:
            if not extension.ownership_acknowledged:
                continue
            changed = dict((str(e.pk), s) for e, s in changes).get(str(extension.pk))
            kind = (
                "review_required"
                if context["chain_state"] == "review"
                else {
                    "active": "activated",
                    "cancelled": "cancelled",
                    "created_unpaid": "created_unpaid",
                }.get(changed, "changed")
            )
            StayExtensionEvent.objects.get_or_create(
                extension=extension,
                revision=chain.revision,
                defaults={"kind": "stay_extension." + kind},
            )
            transaction.on_commit(lambda pk=extension.pk: _schedule(pk), robust=True)


def guest_snapshot(extension):
    draft = extension.draft
    with translation.override(draft.guest_language or "ar"):
        listing_name = extension.chain.property.display_name
    return {
        "name": f"{draft.guest_first_name} {draft.guest_last_name}".strip(),
        "phone": draft.guest_phone,
        "language": draft.guest_language or "ar",
        "listing_name": listing_name,
    }


def refresh_reservation(reservation_id):
    reservation = Reservation.objects.filter(pk=reservation_id).first()
    if not reservation:
        return
    ids = set(
        StayExtension.objects.filter(reservation_id=reservation.pk).values_list(
            "chain_id", flat=True
        )
    )
    if reservation.hostaway_reservation_id:
        roots = StayExtensionChain.objects.filter(
            root_hostaway_reservation_id=reservation.hostaway_reservation_id
        )
        for chain in roots:
            root = chain.root_snapshot
            if (
                reservation.check_in.isoformat() != root["arrival_date"]
                or reservation.check_out.isoformat() != root["departure_date"]
                or reservation.hostaway_listing_map_id != root["listing_map_id"]
                or reservation.channel_id != root["channel_id"]
                or reservation.normalized_status not in Reservation.ACTIVE_STATUSES
            ):
                StayExtensionChain.objects.filter(pk=chain.pk).update(review_required=True)
            ids.add(chain.pk)
    for chain_id in ids:
        refresh_chain(chain_id)


def capture_reservation_change(reservation_id):
    # The extension outbox normally commits with the business change. A broken
    # extension projection must never roll back a verified financial collection.
    try:
        with transaction.atomic():
            refresh_reservation(reservation_id)
    except Exception:
        logger.error("Stay-extension projection needs reconciliation")


def recover_pending(limit=3):
    """Scoped addition to the existing recovery task, never a new schedule."""
    from .stay_extension_communications import deliver_events

    pending = StayExtension.objects.filter(
        state__in=["created_unpaid", "creating"],
        reservation__payment_status="paid",
    ).order_by("updated_at")[:limit]
    for extension in pending:
        capture_reservation_change(extension.reservation_id)
        StayExtension.objects.filter(pk=extension.pk).update(updated_at=timezone.now())
    ids = list(
        StayExtensionEvent.objects.filter(state="pending", attempts__lt=10)
        .order_by("created_at")
        .values_list("extension_id", flat=True)[:limit]
    )
    for extension_id in set(ids):
        deliver_events(extension_id)


def prepare_ownership(reservation, *, client=None, transport=None):
    """Mandatory exact ownership ACK before provider POST. Unrelated stays no-op."""
    from .stay_extension_communications import acknowledge_ownership

    extension = (
        StayExtension.objects.select_related("chain__property", "draft")
        .filter(reservation=reservation)
        .first()
    )
    if extension is None:
        return True
    if (
        not settings.RESERVATION_PAYMENT_LINKS_ENABLED
        or not settings.HYPERPAY_ENABLED
        or settings.HYPERPAY_ENVIRONMENT != "production"
        or reservation.total_price <= 0
        or not reservation.booking_intent.payment_amount_sar
    ):
        StayExtension.objects.filter(pk=extension.pk).update(last_error="payment_path_unavailable")
        return False
    # Another worker may already have received/created this child while this
    # request was checking availability. Never roll its state back to creating.
    current = Reservation.objects.get(pk=reservation.pk)
    if current.hostaway_reservation_id:
        return True
    try:
        raw = read_source(extension.previous_hostaway_reservation_id, client=client)
        snapshot = source_snapshot(raw)
        if (
            snapshot != extension.previous_snapshot
            or snapshot["status"] not in Reservation.ACTIVE_STATUSES
        ):
            raise ValueError("source_changed")
        if reservation.check_in.isoformat() != snapshot["departure_date"]:
            raise ValueError("extension_start_changed")
        # A source can be current on selection and no longer current at execution.
        eligible_source(raw)
        state = chain_context(extension.chain)
        if (
            state["chain_state"] == "review"
            or state["terminal_hostaway_reservation_id"] != snapshot["id"]
        ):
            raise ValueError("chain_changed")
        StayExtension.objects.filter(pk=extension.pk).update(state="creating", last_error="")
        refresh_chain(extension.chain_id)
        if not acknowledge_ownership(extension.pk, transport=transport):
            raise ValueError("ownership_unacknowledged")
    except Exception:
        StayExtension.objects.filter(pk=extension.pk).update(last_error="preflight_requires_review")
        return False
    StayExtension.objects.filter(pk=extension.pk).update(state="creating", last_error="")
    return True


def bind_webhook_child(snapshot):
    """Early Hostaway webhook can bind only the already ACKed local operation."""
    reference = getattr(snapshot, "extension_reference", "")
    if not reference:
        return None
    extension = (
        StayExtension.objects.select_related("reservation", "draft", "chain__property")
        .filter(pk=reference)
        .first()
    )
    if not extension or not extension.ownership_acknowledged or not extension.reservation:
        return None
    reservation = extension.reservation
    if (
        snapshot.listing_map_id != extension.chain.property.hostaway_listing_map_id
        or snapshot.channel_id != settings.HOSTAWAY_DIRECT_CHANNEL_ID
        or snapshot.check_in != extension.draft.check_in
        or snapshot.check_out != extension.draft.check_out
        or snapshot.currency != extension.draft.currency
        or snapshot.total_price != extension.draft.final_total_price
        or reservation.hostaway_reservation_id not in {None, snapshot.reservation_id}
    ):
        return None
    return reservation
