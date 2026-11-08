"""Owner-only recording of paid morning stays in Hostaway, without collecting money."""

from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from apps.accounts.access import require_operations_access
from apps.integrations.hostaway.client import HostawayClient
from apps.properties.models import Property

from .models import MorningStay
from .morning_stays import UNRESOLVED, MorningStayError, active_stays, property_zone, verify_window

KEY = "hostaway_morning"
PROVIDER = "LSA_MorningStay"


def booking_state(stay):
    value = (stay.availability_evidence or {}).get(KEY, {})
    return value if isinstance(value, dict) else {}


def _write_state(stay, value):
    evidence = dict(stay.availability_evidence or {})
    evidence[KEY] = value
    MorningStay.objects.filter(pk=stay.pk).update(availability_evidence=evidence)
    stay.availability_evidence = evidence


def _locked(stay_id):
    property_id = MorningStay.objects.values_list("property_id", flat=True).get(pk=stay_id)
    Property.objects.select_for_update().get(pk=property_id)
    return MorningStay.objects.select_for_update().select_related("property").get(pk=stay_id)


@dataclass(frozen=True)
class MorningRequest:
    payload: dict
    provider: str = PROVIDER

    def to_payload(self):
        return dict(self.payload)


def _request(stay):
    day = stay.starts_at.astimezone(property_zone(stay.property)).date()
    previous = day - timedelta(days=1)
    amount = float(stay.amount_sar)
    first, _, last = stay.guest_name.partition(" ")
    return MorningRequest(
        {
            "channelId": settings.HOSTAWAY_DIRECT_CHANNEL_ID,
            "listingMapId": stay.property.hostaway_listing_map_id,
            "isInitial": 0,
            "isManuallyChecked": 0,
            "guestName": stay.guest_name,
            "guestFirstName": first,
            "guestLastName": last,
            "numberOfGuests": stay.guests,
            "adults": stay.guests,
            "arrivalDate": previous.isoformat(),
            "departureDate": day.isoformat(),
            "checkOutTime": 12,
            "totalPrice": amount,
            "currency": "SAR",
            "financeField": [
                {
                    "type": "price",
                    "name": "baseRate",
                    "title": "Base rate",
                    "alias": "",
                    "quantity": 1,
                    "value": amount,
                    "total": amount,
                    "isIncludedInTotalPrice": 1,
                    "isOverriddenByUser": 1,
                    "isMandatory": None,
                    "isDeleted": 0,
                }
            ],
            # Guest communication remains in the independent MorningStay journey.
            # No card, charge, receipt, email or phone is sent by this recording action.
            "hostNote": (
                f"MorningStay {stay.pk}; paid externally in the website. "
                f"ACTUAL arrival {stay.starts_at.astimezone(property_zone(stay.property)).isoformat()}; "
                f"ACTUAL departure {stay.ends_at.astimezone(property_zone(stay.property)).isoformat()}. "
                "Calendar night is the previous date. Do not collect again."
            ),
        }
    )


def record_in_hostaway(*, request, stay_id, client=None):
    require_operations_access(request)
    if not settings.HOSTAWAY_LIVE_BOOKING_ENABLED or not settings.HOSTAWAY_DIRECT_CHANNEL_ID:
        raise MorningStayError("ربط إنشاء الحجوزات في هوستاوي غير مفعّل.")
    if client is None:
        with HostawayClient(max_get_attempts=1, timeout=10) as remote:
            return record_in_hostaway(request=request, stay_id=stay_id, client=remote)
    with transaction.atomic():
        stay = _locked(stay_id)
        operation = booking_state(stay)
        if operation.get("state") == "confirmed":
            return operation["reservation_id"]
        if operation:
            raise MorningStayError("توجد عملية ربط سابقة؛ راجع نتيجتها ولا تنشئ حجزًا مكررًا.")
        if stay.status != "paid" or not stay.paid_at:
            raise MorningStayError("وثّق استلام كامل مبلغ الحجز الصباحي قبل تسجيله في هوستاوي.")
        if stay.attempts.filter(status__in=UNRESOLVED).exists():
            raise MorningStayError("توجد محاولة دفع غير محسومة؛ تحقق منها أولًا.")
        expected = (stay.starts_at, stay.ends_at, stay.property_id, stay.guests, stay.amount_sar)
    evidence = verify_window(
        stay.property,
        stay.starts_at,
        stay.ends_at,
        guests=stay.guests,
        client=client,
        allow_started=True,
        allow_elapsed=True,
    )
    with transaction.atomic():
        stay = _locked(stay_id)
        operation = booking_state(stay)
        if operation.get("state") == "confirmed":
            return operation["reservation_id"]
        if operation or stay.status != "paid" or not stay.paid_at:
            raise MorningStayError("تغيرت حالة الحجز؛ افتح التفاصيل مجددًا.")
        if expected != (
            stay.starts_at, stay.ends_at, stay.property_id, stay.guests, stay.amount_sar
        ):
            raise MorningStayError("تغيرت تفاصيل الإقامة؛ أعد التحقق قبل التسجيل.")
        if active_stays(
            stay.property, stay.starts_at, stay.ends_at + timedelta(hours=3), exclude=stay.pk
        ).exists():
            raise MorningStayError("توجد إقامة صباحية أخرى متداخلة.")
        payload = _request(stay)
        operation = {
            "state": "creating",
            "requested_at": timezone.now().isoformat(),
            "actor_id": request.user.pk,
            "arrival_date": payload.payload["arrivalDate"],
            "departure_date": payload.payload["departureDate"],
            "amount_sar": str(stay.amount_sar),
            "availability": evidence,
        }
        _write_state(stay, operation)
    # The durable claim commits before POST. Never retry an uncertain creation.
    try:
        snapshot = client.create_reservation_with_price_details(payload).snapshot
    except Exception as exc:
        with transaction.atomic():
            current = _locked(stay_id)
            _write_state(current, operation | {"state": "unknown"})
        raise MorningStayError(
            "نتيجة إنشاء هوستاوي غير محسومة؛ لم نكرر الطلب. راجع هوستاوي قبل أي محاولة أخرى."
        ) from exc
    matches = (
        snapshot.listing_map_id == stay.property.hostaway_listing_map_id
        and snapshot.check_in.isoformat() == operation["arrival_date"]
        and snapshot.check_out.isoformat() == operation["departure_date"]
        and snapshot.guests == stay.guests
        and snapshot.currency == "SAR"
        and snapshot.total_price == Decimal(operation["amount_sar"])
        and snapshot.status.casefold() in {"new", "confirmed", "modified"}
        and snapshot.source == PROVIDER
    )
    with transaction.atomic():
        current = _locked(stay_id)
        _write_state(
            current,
            operation
            | {
                "state": "confirmed" if matches else "review",
                "reservation_id": snapshot.reservation_id,
                "recorded_at": timezone.now().isoformat(),
            },
        )
    if not matches:
        raise MorningStayError(
            "أنشئ سجل في هوستاوي لكن تفاصيله تحتاج مراجعة؛ حُفظ رقمه ومنعنا إنشاء سجل آخر."
        )
    return snapshot.reservation_id


def cancel_linked_stay(stay_id, *, client=None):
    current = MorningStay.objects.get(pk=stay_id)
    operation = booking_state(current)
    if operation.get("state") == "cancelled" and current.status == "cancelled":
        return current
    if operation.get("state") != "confirmed":
        raise MorningStayError("نتيجة الربط غير محسومة؛ راجع هوستاوي قبل الإلغاء.")
    if client is None:
        with HostawayClient(max_get_attempts=1, timeout=10) as remote:
            return cancel_linked_stay(stay_id, client=remote)
    with transaction.atomic():
        current = _locked(stay_id)
        operation = booking_state(current)
        if operation.get("state") != "confirmed":
            raise MorningStayError("توجد عملية إلغاء أخرى؛ راجع حالة الحجز.")
        if current.attempts.filter(status__in=UNRESOLVED).exists():
            raise MorningStayError("توجد محاولة دفع غير محسومة.")
        _write_state(current, operation | {"state": "cancelling"})
    try:
        remote = client.get_reservation(operation["reservation_id"])
        if (
            remote.listing_map_id != current.property.hostaway_listing_map_id
            or remote.source != PROVIDER
            or remote.check_in.isoformat() != operation["arrival_date"]
            or remote.check_out.isoformat() != operation["departure_date"]
        ):
            raise ValueError("morning_booking_identity_mismatch")
        if remote.status.casefold() not in {"cancelled", "canceled"}:
            remote = client.cancel_morning_reservation(operation["reservation_id"])
        if remote.status.casefold() not in {"cancelled", "canceled"}:
            raise ValueError("morning_cancellation_not_confirmed")
    except Exception as exc:
        with transaction.atomic():
            current = _locked(stay_id)
            _write_state(current, operation | {"state": "cancel_unknown"})
        raise MorningStayError("تعذر حسم إلغاء هوستاوي؛ لم نلغ الإقامة محليًا.") from exc
    with transaction.atomic():
        current = _locked(stay_id)
        _write_state(current, operation | {"state": "cancelled"})
        current.status = "cancelled"
        current.save(update_fields=["status", "updated_at"])
        return current
