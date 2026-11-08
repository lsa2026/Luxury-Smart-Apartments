"""Owner-issued, booking-specific past-stay offers. No history campaign or new scheduler."""

from django.conf import settings
from django.core import signing
from django.core.exceptions import ValidationError
from django.utils import timezone

from apps.accounts.models import VerifiedPhone
from apps.core.phone_numbers import normalize_phone_number
from apps.reservations.models import Reservation

from .calculation import amount, cents
from .models import Ledger, Member
from .phone_gifts import phone_hash

KEY = "owner_past_stay_offer_v1"
SALT = "loyalty.owner-past-stay-offer.v1"


def fingerprint(facts):
    from .services import digest

    return digest({k: v for k, v in facts.items() if k not in {"consent", "terms_version", KEY}})


def quote(booking):
    from .site_adapter import booking_facts

    # Quote authoritative persisted decimals/relations, never stale in-memory values.
    booking = Reservation.objects.select_related(
        "booking_intent__customer", "booking_intent__quote", "property"
    ).get(pk=booking.pk)
    if Ledger.objects.filter(reservation=booking).exists():
        raise ValidationError("سُجلت نقاط لهذا الحجز سابقًا؛ لا تُرسل دعوة بمكافأة جديدة.")
    facts, blocker = booking_facts(booking, past_preview=True)
    if blocker:
        detail = {
            "past_completed_stay_required": "لم تنتهِ الإقامة أو أن الحجز ملغى.",
            "outside_program_scope": "الحجز خارج نطاق حجوزات الموقع المؤهلة.",
            "verified_payment_incomplete": "السداد الموثق لا يغطي قيمة الإقامة.",
            "verified_payment_required": "لا توجد دفعة موثقة لهذا الحجز.",
            "price_breakdown_missing": "تفاصيل قيمة الإقامة والرسوم غير مكتملة.",
            "price_breakdown_mismatch": "تفاصيل عرض السعر لا تطابق السعر النهائي الموثق للحجز.",
            "manual_price_breakdown_required": "يلزم توثيق توزيع السعر النهائي للإقامة والرسوم.",
        }.get(blocker, "راجع توثيق السداد وتفاصيل السعر ومواعيد الإقامة.")
        raise ValidationError("هذه الدعوة للإقامات السابقة المؤهلة والمسددّة فقط. " + detail)
    reward = amount(cents(facts["base"]) * 3 // 100)
    if reward <= 0:
        raise ValidationError("لا توجد قيمة إقامة مؤهلة لمكافأة ٣٪.")
    return {
        "total": facts["total"],
        "base": facts["base"],
        "reward": format(reward, ".2f"),
        "fingerprint": fingerprint(facts),
    }


def issue(booking, target, request_id, channel, operator):
    payload = {
        "booking": str(booking.pk),
        "phone": phone_hash(target["phone"]),
        "customer": target["customer"],
        "quote": target["reward"],
        "operator": operator.pk,
        "request_id": request_id,
        "channel": channel,
        "issued_at": timezone.now().isoformat(),
    }
    booking.loyalty_state[KEY] = {
        "proof": signing.dumps(payload, salt=SALT),
        "phone": payload["phone"],
    }


def retain_sent_offer(booking, target):
    """A deliberate repeat must retain the original delivered/ambiguous reward offer."""
    stored = booking.loyalty_state.get(KEY)
    if not stored:
        return False
    try:
        offer = signing.loads(stored["proof"], salt=SALT)
        key = (
            "owner_join_invitation_requests"
            if offer["channel"] == "whatsapp"
            else "owner_join_invitation_email_requests"
        )
        row = booking.loyalty_state.get(key, {}).get(offer["request_id"], {})
        if row.get("status") not in {"accepted", "uncertain"}:
            return False
        if (
            offer["booking"] != str(booking.pk)
            or offer["phone"] != phone_hash(target["phone"])
            or offer["quote"] != target["reward"]
            or (offer["customer"] and offer["customer"] != target["customer"])
        ):
            raise ValueError("changed_offer")
        return True
    except (signing.BadSignature, KeyError, ValueError, TypeError) as exc:
        raise ValidationError("تغير عرض المكافأة أو مستلمه؛ راجعه قبل إعادة الإرسال.") from exc


def authorized(booking):
    try:
        stored = booking.loyalty_state.get(KEY, {})
        offer = signing.loads(stored.get("proof", ""), salt=SALT)
        if offer["booking"] != str(booking.pk):
            return None
        requests_key = (
            "owner_join_invitation_requests"
            if offer["channel"] == "whatsapp"
            else "owner_join_invitation_email_requests"
        )
        row = booking.loyalty_state.get(requests_key, {}).get(offer["request_id"], {})
        if (
            row.get("status") not in {"accepted", "uncertain"}
            or row.get("operator") != offer["operator"]
        ):
            return None
        credited = Ledger.objects.filter(
            reservation=booking,
            event_type="earn",
            component="loyalty",
            **{f"evidence__{KEY}__request_id": offer["request_id"]},
        ).first()
        if credited:
            return (
                {**offer, "beneficiary": credited.customer_id}
                if booking.booking_intent.customer_id in {None, credited.customer_id}
                else None
            )
        phone = normalize_phone_number(booking.booking_intent.guest_phone)
        if offer["phone"] != phone_hash(phone):
            return None
        binding = (
            VerifiedPhone.objects.select_related("user")
            .filter(phone=phone, user__is_active=True, user__member__status="active")
            .first()
        )
        if not binding or (offer["customer"] and offer["customer"] != binding.user_id):
            return None
        if booking.booking_intent.customer_id not in {None, binding.user_id}:
            return None
        return {**offer, "beneficiary": binding.user_id}
    except (signing.BadSignature, KeyError, ValueError, TypeError, AttributeError):
        return None


def apply_for_user(user):
    """Only explicit accepted/ambiguous owner offers, and only the verified phone owner."""
    from .services import posting_transaction
    from .site_adapter import _safe_reconcile

    if (
        not settings.LOYALTY_ENABLED
        or not user.is_active
        or not Member.objects.filter(customer=user, status="active").exists()
    ):
        return
    phones = list(VerifiedPhone.objects.filter(user=user).values_list("phone", flat=True))
    if not phones:
        return
    for booking_id in Reservation.objects.filter(
        **{f"loyalty_state__{KEY}__phone__in": [phone_hash(p) for p in phones]}
    ).values_list("pk", flat=True):
        with posting_transaction():
            booking = Reservation.objects.select_related("booking_intent", "property").get(
                pk=booking_id
            )
            offer = authorized(booking)
            if offer and offer["beneficiary"] == user.pk:
                _safe_reconcile(booking.pk)


def apply_for_booking(booking_id):
    booking = Reservation.objects.select_related("booking_intent").get(pk=booking_id)
    if not booking.booking_intent_id:
        return
    try:
        phone = normalize_phone_number(booking.booking_intent.guest_phone)
    except ValueError:
        return
    binding = VerifiedPhone.objects.select_related("user").filter(phone=phone).first()
    if binding:
        apply_for_user(binding.user)
