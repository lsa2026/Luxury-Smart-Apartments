"""Private reporting classification; contact matches never grant account access."""

from dataclasses import dataclass
from functools import lru_cache

from django.core.exceptions import ValidationError
from django.core.validators import validate_email

from apps.accounts.access import normalize_email
from apps.reservations.models import DayUseStay, MorningStay, Reservation
from apps.reservations.phone_search import phone_variants


@dataclass(frozen=True)
class GuestClassification:
    code: str
    label: str
    basis: str


def guest_classifications():
    """One streaming SELECT per source, independent of search/the visible 100.

    Classify the guest across distinct confirmed bookings, not their account,
    name, payment attempts, or order in the current list. The same Hostaway
    reservation projected by two local paths counts only once. No writes,
    provider calls, persistent contact cache or marketing exports.
    """

    @lru_cache(maxsize=4096)
    def email_key(value):
        normalized = normalize_email(value)
        try:
            validate_email(normalized)
        except ValidationError:
            return ""
        return normalized

    @lru_cache(maxsize=4096)
    def phone_key(value):
        return next(
            (number for number in phone_variants(value or "") if number.startswith("+")), ""
        )

    email_bookings, phone_bookings = {}, {}
    contacts = {}

    def record(reference, email, phone):
        # Malformed historical evidence must not manufacture a real booking.
        if isinstance(reference, bool) or not str(reference or "").isascii():
            return "", ""
        try:
            if not str(reference).isdigit() or int(reference) <= 0:
                return "", ""
            identity = str(int(reference))
        except (ValueError, TypeError):
            return "", ""
        keys = email_key(email), phone_key(phone)
        for index, key in zip((email_bookings, phone_bookings), keys, strict=True):
            if key:
                seen = index.setdefault(key, set())
                if len(seen) < 2:
                    seen.add(identity)
        return keys

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
        contacts[pk] = record(reference, email, phone)
    for model, key in ((MorningStay, "hostaway_morning"), (DayUseStay, "hostaway_day_use")):
        rows = (
            model.objects.filter(
                environment="production",
                **{f"availability_evidence__{key}__state": "confirmed"},
            )
            .exclude(status="cancelled")
            .order_by()
            .values_list(
                f"availability_evidence__{key}__reservation_id", "guest_email", "guest_phone"
            )
        )
        for reference, email, phone in rows.iterator(chunk_size=500):
            record(reference, email, phone)

    result = {}
    for pk, (email, phone) in contacts.items():
        matched_email = bool(email and len(email_bookings[email]) > 1)
        matched_phone = bool(phone and len(phone_bookings[phone]) > 1)
        if matched_email or matched_phone:
            basis = (
                "تطابق البريد والجوال"
                if matched_email and matched_phone
                else "تطابق البريد الإلكتروني"
                if matched_email
                else "تطابق رقم الجوال"
            )
            result[pk] = GuestClassification("returning", "ضيف عائد", basis)
        elif email or phone:
            result[pk] = GuestClassification(
                "new", "ضيف جديد", "لا يوجد حجز آخر مطابق في السجل المتاح"
            )
        else:
            result[pk] = GuestClassification(
                "unknown", "بيانات غير كافية", "يلزم بريد أو جوال صالح للمطابقة"
            )
    return result
