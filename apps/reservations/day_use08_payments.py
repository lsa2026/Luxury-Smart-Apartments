"""Prepared, opt-in financial handoff. Never replay the booking communications."""

import hashlib
import json
from datetime import timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import httpx
from django.conf import settings
from django.core import signing
from django.db import transaction
from django.http import Http404, JsonResponse
from django.utils import timezone
from django.views.decorators.cache import never_cache
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_POST

from apps.core.phone_numbers import InvalidPhoneNumber, normalize_phone_number
from apps.loyalty.models import JoinInvitation
from apps.notifications.services.ultramsg import UltraMsgClient

from .day_use08_gateway import exact_receipt_messages
from .day_use_hostaway import booking_state, hostaway_first
from .models import DayUse08PaymentDelivery, DayUse08PaymentEvent, DayUseStay

HEADER = "X-LSA-DayUse08-Payment-Token"
KINDS = ("payment_success", "loyalty_invitation")
DEFERRED = {
    "programme_unavailable",
    "core_pending",
    "payment_notice_pending",
    "receipt_lookup_uncertain",
    "eligibility_unverified",
}
READ_SALT = "day_use08.payment-event.read.v1"
SEND_SALT = "day_use08.payment-event.delivery.v1"


def enabled():
    return settings.DAY_USE08_ENABLED


def owns_invitation(stay):
    # A rollback of the flag must not restore the old sender for claimed stays.
    return settings.DAY_USE08_ENABLED or stay.payment_events.exists()


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def reference(event_id, kind):
    key = f"lsa08pay:{event_id}:{kind}"
    return key, "lsa08pay-" + hashlib.md5(key.encode(), usedforsecurity=False).hexdigest()


def verified_facts(stay):
    """One full collection, no competing or unresolved money, confirmed Hostaway."""
    operation = booking_state(stay)
    if (
        stay.environment != "production"
        or stay.status != "paid"
        or not stay.paid_at
        or stay.ends_at <= timezone.now()
        or not stay.communication_revision
        or not hostaway_first(stay)
        or operation.get("state") != "confirmed"
        or not operation.get("reservation_id")
        or stay.attempts.filter(status__in=["created", "pending", "unknown", "review"]).exists()
    ):
        return None
    attempts = list(stay.attempts.filter(status="succeeded"))
    receipt = getattr(stay, "offline_receipt", None)
    if receipt and not attempts:
        if (
            receipt.amount != stay.amount_sar
            or not receipt.recorded_by_id
            or stay.paid_at != receipt.received_at
        ):
            return None
        kind, proof = (
            "admin_receipt",
            [str(receipt.pk), str(receipt.amount), receipt.received_at.isoformat()],
        )
    elif not receipt and len(attempts) == 1:
        attempt = attempts[0]
        if (
            attempt.amount != stay.amount_sar
            or attempt.currency != "SAR"
            or not attempt.verified_at
            or not attempt.provider_payment_id
            or stay.paid_at != attempt.verified_at
        ):
            return None
        kind, proof = (
            "hyperpay_verified",
            [
                str(attempt.pk),
                str(attempt.amount),
                attempt.verified_at.isoformat(),
                attempt.provider_payment_id,
            ],
        )
    else:
        return None
    try:
        phone = normalize_phone_number(stay.guest_phone)
    except InvalidPhoneNumber:
        return None
    return {
        "source": "day_use08_payment",
        "contract_version": 1,
        "event_type": "payment_succeeded",
        "day_use_stay_id": str(stay.pk),
        "booking_revision": stay.communication_revision,
        "hostaway_reservation_id": operation["reservation_id"],
        "listing_map_id": stay.property.hostaway_listing_map_id,
        "listing_name": getattr(stay.property, "name_" + stay.guest_language, "")
        or stay.property.name_en,
        "payment_verified": True,
        "payment_status": "paid",
        "proof_kind": kind,
        "proof_key": digest([str(stay.pk), kind, proof]),
        "amount_sar": f"{stay.amount_sar:.2f}",
        "currency": "SAR",
        "paid_at": stay.paid_at.isoformat(),
        "guest_name": stay.guest_name,
        "phone": phone,
        "guest_email": stay.guest_email,
        "guest_language": stay.guest_language,
        "starts_at": stay.starts_at.isoformat(),
        "ends_at": stay.ends_at.isoformat(),
        "timezone": stay.time_zone,
        "management_url": settings.SITE_BASE_URL.rstrip("/")
        + f"/admin/operations/day-use-stays/{stay.pk}/",
    }


@transaction.atomic
def capture_payment(stay_id):
    if not enabled():
        return None
    stay = (
        DayUseStay.objects.select_for_update(of=("self",))
        .select_related("property")
        .get(pk=stay_id)
    )
    facts = verified_facts(stay)
    if not facts:
        return None
    prior = stay.payment_events.filter(proof_key=facts["proof_key"]).first()
    if prior:
        return prior
    revision = (
        stay.payment_events.order_by("-payment_revision")
        .values_list("payment_revision", flat=True)
        .first()
        or 0
    ) + 1
    event = DayUse08PaymentEvent(
        stay=stay,
        payment_revision=revision,
        proof_key=facts["proof_key"],
        proof_kind=facts["proof_kind"],
    )
    event.payload = {**facts, "payment_event_id": str(event.pk), "payment_revision": revision}
    event.save()
    transaction.on_commit(lambda: queue_event(str(event.pk)), robust=True)
    return event


def queue_event(event_id):
    from .tasks import deliver_day_use08_payment

    deliver_day_use08_payment.delay(event_id)


def capability(event, *, delivery=False):
    return signing.TimestampSigner(salt=SEND_SALT if delivery else READ_SALT).sign(str(event.pk))


def authorize(request, *, event_id=None, delivery=False):
    if not enabled():
        raise Http404
    try:
        actual = signing.TimestampSigner(salt=SEND_SALT if delivery else READ_SALT).unsign(
            request.headers.get(HEADER, ""), max_age=15 * 86400
        )
        event = DayUse08PaymentEvent.objects.select_related("stay__property").get(pk=actual)
    except (signing.BadSignature, DayUse08PaymentEvent.DoesNotExist, ValueError) as exc:
        raise Http404 from exc
    if event_id is not None and str(event_id) != str(event.pk):
        raise Http404
    return event


def current(event):
    facts = verified_facts(event.stay)
    return bool(facts and all(event.payload.get(k) == v for k, v in facts.items()))


@require_GET
@never_cache
def read_event(request, event_id):
    event = authorize(request, event_id=event_id)
    if not current(event):
        return JsonResponse({"code": "stale"}, status=409)
    return JsonResponse(
        {
            **event.payload,
            "payment_token": capability(event, delivery=True),
            "jobs": {
                kind: {
                    "job_key": reference(event.pk, kind)[0],
                    "reference_id": reference(event.pk, kind)[1],
                }
                for kind in KINDS
            },
        }
    )


def render_success(stay):
    from apps.core.models import SiteSetting

    site = SiteSetting.objects.first()
    support = (
        (site.whatsapp_display_number or site.contact_phone) if site else ""
    ) or settings.WHATSAPP_CONTACT_NUMBER
    lang = stay.guest_language
    labels = {
        "ar": (
            "تم استلام دفعتك بنجاح",
            "لوكسري سمارت أبارتمنتس",
            "مرحبًا",
            "تم استلام سداد حجزك النهاري.",
            "الشقة",
            "الدخول",
            "الخروج",
            "المبلغ",
            "المرجع",
            "للمساعدة",
            "نتمنى لك إقامة سعيدة 🌿",
        ),
        "en": (
            "Payment received successfully",
            "Luxury Smart Apartments",
            "Hello",
            "We received payment for your daytime stay.",
            "Apartment",
            "Check-in",
            "Check-out",
            "Amount",
            "Reference",
            "Support",
            "Enjoy your stay 🌿",
        ),
        "fr": (
            "Paiement reçu avec succès",
            "Luxury Smart Apartments",
            "Bonjour",
            "Nous avons reçu le paiement de votre séjour en journée.",
            "Appartement",
            "Arrivée",
            "Départ",
            "Montant",
            "Référence",
            "Assistance",
            "Bon séjour 🌿",
        ),
    }[lang]
    title, brand, hello, note, apartment, arrival, departure, amount, ref, help_label, close = (
        labels
    )
    name = getattr(stay.property, "name_" + lang, "") or stay.property.name_en
    start, end = (
        x.astimezone(ZoneInfo(stay.time_zone)).strftime("%Y-%m-%d %H:%M")
        for x in (stay.starts_at, stay.ends_at)
    )
    lines = [
        f"*{title}* ✅",
        "",
        f"*{brand} 🌿*",
        "",
        f"{hello} {stay.guest_name}،",
        note,
        "",
        f"🏡 *{apartment}:* {name}",
        f"🕐 *{arrival}:* {start}",
        f"🕛 *{departure}:* {end}",
        f"💳 *{amount}:* {stay.amount_sar:.2f} SAR",
        f"*{ref}:* DU-{str(stay.pk)[:8]}",
        "",
        "──────────",
    ]
    if support:
        lines.append(f"{help_label}: {support}")
    return "\n".join([*lines, close])


def delivered(messages):
    return any(
        str(m.get("ack", "")).lower() in {"2", "3", "4", "device", "delivered", "read", "played"}
        or str(m.get("status", "")).lower() in {"delivered", "read", "played"}
        for m in messages
    )


def receipts(attempt):
    return exact_receipt_messages(
        SimpleNamespace(provider_id=attempt.provider_id, reference_id=attempt.provider_reference)
    )


def invitation_allowed(event_id, stay):
    event = (
        DayUse08PaymentEvent.objects.select_related("stay__property")
        .filter(pk=event_id, stay=stay)
        .first()
    )
    if not enabled() or not event or not current(event):
        return False
    notice = event.deliveries.filter(message_type="payment_success").first()
    return bool(notice and delivered(receipts(notice)))


def result(attempt, *, duplicate=False):
    return {
        "code": "duplicate" if duplicate else attempt.response.get("code", attempt.state),
        "sent": attempt.state in {"accepted", "delivered", "read"},
        "state": attempt.state,
        "reference_id": attempt.reference_id,
        "provider_message_id": attempt.provider_id or None,
        "provider_reference": attempt.provider_reference or None,
    }


@csrf_exempt
@require_POST
@never_cache
def send(request):
    event = authorize(request, delivery=True)
    try:
        body = json.loads(request.body) if len(request.body) <= 1024 else None
        kind = body["message_type"]
        key, ref = reference(event.pk, kind)
        if (
            kind not in KINDS
            or set(body) != {"message_type", "job_key", "reference_id"}
            or body["job_key"] != key
            or body["reference_id"] != ref
        ):
            raise ValueError
    except (ValueError, KeyError, TypeError):
        return JsonResponse({"code": "invalid_request", "sent": False}, status=400)
    # All financial and delivery eligibility checks happen BEFORE a provider claim.
    from . import day_use_loyalty

    with transaction.atomic():
        event.stay = (
            DayUseStay.objects.select_for_update(of=("self",))
            .select_related("property")
            .get(pk=event.stay_id)
        )
        prior = event.deliveries.filter(pk=ref).first()
        if prior and prior.state != "deferred":
            return JsonResponse(result(prior, duplicate=True))
        if not current(event):
            return JsonResponse({"code": "stale", "sent": False}, status=409)
        try:
            if not day_use_loyalty.core_delivered(
                event.stay, event.payload["booking_revision"], reconcile_uncertain=True
            ):
                return JsonResponse({"code": "core_pending", "sent": False})
            if kind == "loyalty_invitation" and not invitation_allowed(event.pk, event.stay):
                return JsonResponse({"code": "payment_notice_pending", "sent": False})
        except ValueError:
            return JsonResponse({"code": "receipt_lookup_uncertain", "sent": False})
        if prior:
            prior.state, prior.response = "posting", {}
            prior.save(update_fields=["state", "response", "updated_at"])
            attempt = prior
        else:
            attempt = DayUse08PaymentDelivery.objects.create(
                reference_id=ref,
                event=event,
                message_type=kind,
                provider_reference=ref if kind == "payment_success" else "",
            )
    # The committed posting claim is never repeated, even after a lost response.
    state, provider_id, provider_ref = "unknown", "", ref
    try:
        if kind == "payment_success":
            with UltraMsgClient() as provider:
                response = provider.send_text(
                    recipient=event.payload["phone"],
                    body=render_success(event.stay),
                    reference_id=ref,
                )
            state, provider_id = "accepted", str(response.get("id") or "")
        else:
            response = json.loads(
                day_use_loyalty.invite_paid_booking(
                    event.stay,
                    event.payload["booking_revision"],
                    payment_event_id=event.pk,
                    payment_delivery_id=ref,
                ).content
            )
            state = response["code"]
            row = JoinInvitation.objects.filter(
                reservation_key=__import__(
                    "apps.loyalty.invitation_registry", fromlist=["day_use_booking_key"]
                ).day_use_booking_key(event.stay),
                phone=event.payload["phone"],
            ).first()
            if row and row.dispatched_at:
                provider_id, provider_ref = row.provider_id, row.provider_reference
            else:
                provider_ref = ""
    except Exception:
        # Provider acceptance may have happened. Resolve by exact receipt only.
        state = "unknown"
        attempt.refresh_from_db()
        provider_ref = attempt.provider_reference
    code = state
    if state in DEFERRED:
        state = "deferred"
    elif state in {"member", "duplicate"}:
        state = "skipped"
    elif state not in {"accepted", "failed", "unknown"}:
        state = "blocked"
    DayUse08PaymentDelivery.objects.filter(pk=attempt.pk).exclude(
        state__in=["delivered", "read"]
    ).update(
        state=state,
        provider_id=provider_id,
        provider_reference=provider_ref,
        response={"code": code},
    )
    attempt.refresh_from_db()
    return JsonResponse(result(attempt))


@require_GET
@never_cache
def receipt(request):
    event = authorize(request, delivery=True)
    if set(request.GET) != {"reference_id"}:
        return JsonResponse({"code": "invalid_request"}, status=400)
    attempt = event.deliveries.filter(pk=request.GET["reference_id"]).first()
    if not attempt:
        raise Http404
    try:
        messages = receipts(attempt) if attempt.provider_reference else []
    except ValueError:
        return JsonResponse({"code": "receipt_lookup_uncertain", "messages": []}, status=503)
    if delivered(messages):
        DayUse08PaymentDelivery.objects.filter(pk=attempt.pk).exclude(state="read").update(
            state="delivered"
        )
        if attempt.message_type == "loyalty_invitation":
            from apps.loyalty import invitation_registry as registry

            with registry.invitation_lock():
                row = JoinInvitation.objects.filter(
                    reservation_key=__import__(
                        "apps.loyalty.invitation_registry", fromlist=["day_use_booking_key"]
                    ).day_use_booking_key(event.stay),
                    phone=event.payload["phone"],
                ).first()
                if row and row.provider_reference == attempt.provider_reference:
                    registry.record(row, "delivered", attempt.provider_id)
    return JsonResponse({"code": "ok", "reference_id": attempt.pk, "messages": messages})


def deliver_event(event_id, *, client=None):
    """Retry only the webhook envelope; guest sends have a different durable claim."""
    if not enabled() or not settings.DAY_USE08_PAYMENT_WEBHOOK_URL:
        return {"accepted": False}
    now = timezone.now()
    with transaction.atomic():
        event = (
            DayUse08PaymentEvent.objects.select_for_update(of=("self",))
            .select_related("stay__property")
            .get(pk=event_id)
        )
        if (
            event.state != "pending"
            or event.next_attempt_at > now
            or (event.lease_until and event.lease_until > now)
        ):
            return {"accepted": event.state == "accepted"}
        if not current(event):
            event.state, event.last_error = "stale", "payment_source_changed"
            event.save(update_fields=["state", "last_error"])
            return {"accepted": False}
        event.attempts += 1
        lease = now + timedelta(seconds=45)
        event.lease_until, event.next_attempt_at = lease, now + timedelta(seconds=30)
        event.save(update_fields=["attempts", "lease_until", "next_attempt_at"])
    envelope = {"payment_event_id": str(event.pk), "event_token": capability(event)}
    try:
        if client is None:
            with httpx.Client(
                timeout=httpx.Timeout(15, connect=5), follow_redirects=False
            ) as remote:
                response = remote.post(settings.DAY_USE08_PAYMENT_WEBHOOK_URL, json=envelope)
        else:
            response = client.post(settings.DAY_USE08_PAYMENT_WEBHOOK_URL, json=envelope)
        body = response.json() if response.status_code in {200, 202} else {}
        ok = (
            isinstance(body, dict)
            and body.get("accepted") is True
            and body.get("payment_event_id") == str(event.pk)
        )
    except (httpx.HTTPError, ValueError):
        ok = False
    updates = {"lease_until": None, "last_error": "" if ok else "n8n_persistence_not_acknowledged"}
    if ok:
        updates.update(state="accepted", accepted_at=timezone.now())
    DayUse08PaymentEvent.objects.filter(pk=event.pk, state="pending", lease_until=lease).update(
        **updates
    )
    return {"accepted": ok}
