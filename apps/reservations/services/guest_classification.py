"""Private Hostaway-based reporting; matches never grant account access."""

from dataclasses import dataclass

from django.core.exceptions import ValidationError
from django.core.validators import validate_email
from django.utils.crypto import salted_hmac

from apps.accounts.access import normalize_email
from apps.reservations.models import Reservation
from apps.reservations.phone_search import phone_variants


@dataclass(frozen=True)
class GuestClassification:
    code: str
    label: str
    basis: str


def contact_keys(email, phone):
    """Only keyed contact digests enter the private shared cache."""
    email = normalize_email(email)
    try:
        validate_email(email)
    except ValidationError:
        email = ""
    phone = next((value for value in phone_variants(phone or "") if value.startswith("+")), "")
    return tuple(
        salted_hmac(f"guest-classification-{kind}.v1", value, algorithm="sha256").hexdigest()
        if value
        else ""
        for kind, value in (("email", email), ("phone", phone))
    )


def guest_classifications():
    """Compare local card identities exclusively against a complete Hostaway scan."""
    from .guest_classification_history import provider_snapshot

    snapshot = provider_snapshot()
    result = {}
    rows = (
        Reservation.objects.filter(
            booking_intent__isnull=False,
            normalized_status__in=Reservation.ACTIVE_STATUSES,
            hostaway_reservation_id__gt=0,
            is_test=False,
        )
        .order_by()
        .values_list(
            "pk",
            "hostaway_reservation_id",
            "booking_intent__guest_email",
            "booking_intent__guest_phone",
        )
    )
    for pk, reference, email, phone in rows.iterator(chunk_size=500):
        email, phone = contact_keys(email, phone)
        if not (email or phone):
            result[pk] = GuestClassification(
                "unknown", "بيانات غير كافية", "يلزم بريد أو جوال صالح للمطابقة"
            )
            continue
        if snapshot is None:
            result[pk] = GuestClassification("unknown", "غير متحقق", "لم يكتمل فحص سجلات هوستاوي")
            continue
        matched_email = bool(
            email and any(value != reference for value in snapshot["email"].get(email, []))
        )
        matched_phone = bool(
            phone and any(value != reference for value in snapshot["phone"].get(phone, []))
        )
        if matched_email or matched_phone:
            basis = (
                "تطابق البريد والجوال"
                if matched_email and matched_phone
                else "تطابق البريد الإلكتروني"
                if matched_email
                else "تطابق رقم الجوال"
            )
            result[pk] = GuestClassification("returning", "ضيف عائد", basis + " في هوستاوي")
        else:
            result[pk] = GuestClassification(
                "new", "ضيف جديد", "لا يوجد حجز آخر مطابق في سجلات هوستاوي المتحققة"
            )
    return result
