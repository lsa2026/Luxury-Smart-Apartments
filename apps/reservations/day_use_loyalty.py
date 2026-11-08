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
from apps.loyalty.enrollment_invitation import invitation_image
from apps.loyalty.invitation_api import ready
from apps.notifications.services.ultramsg import (
    UltraMsgClient,
    UltraMsgConfigurationError,
    UltraMsgResponseError,
)

from .day_use08_gateway import authorize, exact_receipt_messages
from .models import DayUse08DeliveryAttempt, DayUseStay


def caption(stay):
    lang = stay.guest_language
    # Keep the approved artwork, but promise nothing for this hourly stay.
    url = settings.SITE_BASE_URL.rstrip("/") + "/my-bookings/rewards/"
    qualification = {
        "ar": (
            "يسعدنا دعوتك للانضمام مجانًا واختياريًا إلى برنامج مكافآت ضيوفنا. "
            "لا تتضمن هذه الدعوة وعدًا بنقاط عن الإقامة النهارية الحالية؛ "
            "تسري الشروط على الإقامات المستقبلية المؤهلة. اقرأ الشروط ووافق عليها بنفسك "
            "عند رغبتك في الانضمام."
        ),
        "en": (
            "You are invited to join our guest rewards programme free of charge. Joining is "
            "optional. This invitation does not promise points for the current daytime stay; "
            "the terms apply to eligible future stays. Please read and accept the terms yourself "
            "if you choose to join."
        ),
        "fr": (
            "Nous vous invitons à rejoindre gratuitement notre programme de récompenses. "
            "L’adhésion est facultative. Cette invitation ne promet aucun point pour le séjour "
            "de jour actuel ; les conditions concernent les futurs séjours éligibles. "
            "Lisez et acceptez vous-même les conditions si vous souhaitez adhérer."
        ),
    }
    text = qualification[lang] + "\n\n" + url
    if len(text.encode("utf-16-le")) // 2 > 1024:
        raise ValidationError("Invitation caption exceeds the provider limit.")
    return text


def core_delivered(stay, revision, *, reconcile_uncertain=False):
    phone = "".join(c for c in stay.guest_phone if c.isdigit())
    stamp = int(stay.starts_at.timestamp() * 1000)
    guide = f"guide:{stay.property.hostaway_listing_map_id}:{stamp}:{phone}"
    attempts = []
    for suffix in (f"r{revision}:status", "rules:" + phone, guide):
        reference = (
            "lsa08-"
            + hashlib.md5(f"lsa08:{stay.pk}:{suffix}".encode(), usedforsecurity=False).hexdigest()
        )
        attempt = DayUse08DeliveryAttempt.objects.filter(
            pk=reference,
            stay=stay,
            state__in=["accepted", "posting", "uncertain"] if reconcile_uncertain else ["accepted"],
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
    return invite_paid_booking(stay, revision, core_claimed=body["core_delivered"])


def invite_paid_booking(
    stay, revision, *, core_claimed=True, payment_event_id=None, payment_delivery_id=None
):
    from .day_use08_payments import invitation_allowed, verified_facts

    if payment_event_id is None or payment_delivery_id is None:
        return JsonResponse({"code": "managed_by_day_use08"})
    if payment_event_id is not None:
        try:
            if not invitation_allowed(payment_event_id, stay):
                return JsonResponse({"code": "payment_notice_pending"})
        except ValueError:
            return JsonResponse({"code": "receipt_lookup_uncertain"})

    if stay.status != "paid" or stay.ends_at <= timezone.now() or not verified_facts(stay):
        return JsonResponse({"code": "not_eligible"})
    if not core_claimed:
        return JsonResponse({"code": "core_pending"})
    if not ready():
        return JsonResponse({"code": "programme_unavailable"})
    phone = registry.normalize_phone(stay.guest_phone)
    try:
        key = registry.day_use_booking_key(stay)
    except ValidationError:
        return JsonResponse({"code": "not_eligible"})
    with registry.invitation_lock():
        if registry.member_exists(phone, stay.guest_email):
            return JsonResponse({"code": "member"})
        prior = registry.competing(key, phone, stay.guest_email)
        if prior:
            return JsonResponse({"code": "duplicate", "status": prior.state})
    try:
        if not core_delivered(stay, revision, reconcile_uncertain=True):
            return JsonResponse({"code": "core_pending"})
        content, text = invitation_image(stay.guest_language), caption(stay)
        with registry.invitation_lock():
            stay = DayUseStay.objects.select_for_update().get(pk=stay.pk)
            if stay.communication_revision != revision or stay.status != "paid":
                return JsonResponse({"code": "stale"})
            from .models import DayUse08PaymentDelivery

            if not DayUse08PaymentDelivery.objects.filter(
                pk=payment_delivery_id,
                event_id=payment_event_id,
                event__stay=stay,
                message_type="loyalty_invitation",
                state="posting",
            ).exists():
                return JsonResponse({"code": "stale"})
            decision, row = registry.claim(
                key=key,
                phone=phone,
                email=stay.guest_email,
                customer_id=None,
                language=stay.guest_language,
                event_id=key + ":paid-invitation-v1",
                source="day_use08",
                origin="website",
                channel="whatsapp",
            )
            if decision != "eligible":
                return JsonResponse({"code": decision})
            if (
                registry.consume(
                    row,
                    phone=phone,
                    day_use_stay=stay,
                    day_use_payment_event_id=payment_event_id,
                )
                != "eligible"
            ):
                return JsonResponse({"code": "duplicate"})
            if payment_event_id and payment_delivery_id:
                from .models import DayUse08PaymentDelivery

                # Commit the real image reference with the registry send permit.
                # A crash before the HTTP response must still be reconcilable.
                updated = DayUse08PaymentDelivery.objects.filter(
                    pk=payment_delivery_id,
                    event_id=payment_event_id,
                    message_type="loyalty_invitation",
                    event__stay=stay,
                ).update(provider_reference=row.provider_reference)
                if updated != 1:
                    raise ValidationError("day_use_delivery_changed")
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


def queue_paid_invitation(stay):
    """Compatibility hook only: the day-use financial outbox is the sole sender."""
    return None
