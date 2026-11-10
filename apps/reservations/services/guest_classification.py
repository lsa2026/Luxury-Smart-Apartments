"""Private Hostaway-based reporting; matches never grant account access."""

from dataclasses import dataclass

from django.core.exceptions import ValidationError
from django.core.validators import validate_email
from django.utils.crypto import salted_hmac

from apps.accounts.access import normalize_email
from apps.core.phone_numbers import InvalidPhoneNumber, normalize_comparison_phone
from apps.reservations.models import Reservation


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
    try:
        phone = normalize_comparison_phone(phone or "")
    except InvalidPhoneNumber:
        phone = ""
    return tuple(
        salted_hmac(f"guest-classification-{kind}.v1", value, algorithm="sha256").hexdigest()
        if value
        else ""
        for kind, value in (("email", email), ("phone", phone))
    )


def guest_classifications():
    """Both current identity and comparison history come from Hostaway."""
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
        )
    )
    for pk, reference in rows.iterator(chunk_size=500):
        result[pk] = classify_reference(reference, snapshot)
    return result


def classify_reference(reference, snapshot):
    if snapshot is None:
        return GuestClassification("unknown", "غير متحقق", "لم يكتمل فحص سجلات هوستاوي")
    identity = snapshot["identities"].get(str(reference))
    if identity is None:
        return GuestClassification("unknown", "غير متحقق", "يلزم تحديث فحص هوستاوي لهذا الحجز")
    return classify_keys(*identity, snapshot=snapshot, exclude=reference)


def classify_keys(email, phone, *, snapshot, exclude=None):
    if not (email or phone):
        return GuestClassification("unknown", "بيانات غير كافية", "يلزم بريد أو جوال صالح للمطابقة")
    matched_email = bool(
        email and any(value != exclude for value in snapshot["email"].get(email, []))
    )
    matched_phone = bool(
        phone and any(value != exclude for value in snapshot["phone"].get(phone, []))
    )
    if matched_email or matched_phone:
        basis = (
            "تطابق البريد والجوال الدولي"
            if matched_email and matched_phone
            else "تطابق البريد الإلكتروني"
            if matched_email
            else "تطابق رقم الجوال الدولي الكامل"
        )
        return GuestClassification("returning", "ضيف عائد", basis + " في هوستاوي")
    return GuestClassification("new", "ضيف جديد", "لا يوجد حجز آخر مطابق في سجلات هوستاوي المتحققة")


def classify_prospective(email, phone, snapshot):
    """Admin input before creation; this is not an account-ownership decision."""
    if snapshot is None:
        return GuestClassification("unknown", "غير متحقق", "لم يكتمل فحص سجلات هوستاوي")
    result = classify_keys(*contact_keys(email, phone), snapshot=snapshot)
    if result.code == "new" and not snapshot["identities"]:
        return GuestClassification(
            "unknown", "غير متحقق", "لم يتأكد ربط قراءة هوستاوي بالحجوزات القائمة"
        )
    return result
