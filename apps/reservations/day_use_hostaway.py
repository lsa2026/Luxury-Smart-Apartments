"""Owner-only recording of paid day_use stays in Hostaway, without collecting money."""

from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from apps.accounts.access import require_operations_access
from apps.integrations.hostaway.client import HostawayClient
from apps.integrations.hostaway.exceptions import HostawayError
from apps.properties.models import Property

from . import day_use_ownership as ownership
from .day_use_stays import UNRESOLVED, DayUseStayError, active_stays, property_zone, verify_window
from .models import DayUseHostawayOwnership, DayUseStay

KEY = "hostaway_day_use"
PROVIDER = "LSA_DayUse"
HOSTAWAY_FIRST = "hostaway_first_v2"


def hostaway_first(stay):
    return (stay.availability_evidence or {}).get("creation_mode") == HOSTAWAY_FIRST


def linked_for_communication(stay):
    operation = booking_state(stay)
    return hostaway_first(stay) and operation.get("state") in {"confirmed", "cancelled"}


def booking_state(stay):
    value = (stay.availability_evidence or {}).get(KEY, {})
    return value if isinstance(value, dict) else {}


def _write_state(stay, value):
    evidence = dict(stay.availability_evidence or {})
    evidence[KEY] = value
    DayUseStay.objects.filter(pk=stay.pk).update(availability_evidence=evidence)
    stay.availability_evidence = evidence


def _locked(stay_id):
    property_id = DayUseStay.objects.values_list("property_id", flat=True).get(pk=stay_id)
    Property.objects.select_for_update().get(pk=property_id)
    return DayUseStay.objects.select_for_update().select_related("property").get(pk=stay_id)


@dataclass(frozen=True)
class DayUseRequest:
    payload: dict
    provider: str = PROVIDER

    def to_payload(self):
        return dict(self.payload)


def _request(stay):
    amount = float(stay.amount_sar)
    first, _, last = stay.guest_name.partition(" ")
    actual_start = stay.starts_at.astimezone(property_zone(stay.property)).isoformat()
    actual_end = stay.ends_at.astimezone(property_zone(stay.property)).isoformat()
    return DayUseRequest(
        {
            "channelId": settings.HOSTAWAY_DIRECT_CHANNEL_ID,
            "listingMapId": stay.property.hostaway_listing_map_id,
            "isInitial": 0,
            "isManuallyChecked": 0,
            "guestName": stay.guest_name,
            "guestFirstName": first,
            "guestLastName": last,
            "guestEmail": stay.guest_email,
            "phone": stay.guest_phone,
            "guestLocale": stay.guest_language,
            "guestAddress": (stay.billing_address or {}).get("billing_street1", ""),
            "guestCity": (stay.billing_address or {}).get("billing_city", ""),
            "guestZipCode": (stay.billing_address or {}).get("billing_postcode", ""),
            "guestCountry": (stay.billing_address or {}).get("billing_country", ""),
            "numberOfGuests": stay.guests,
            "adults": stay.guests,
            "arrivalDate": stay.inventory_arrival_date.isoformat(),
            "departureDate": stay.inventory_departure_date.isoformat(),
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
            # Guest communication remains in the independent DayUseStay journey.
            # Contact details are booking data; no card or collection is sent.
            "hostNote": (
                f"DU08:{stay.pk}; "
                + ("Payment recorded in website. " if stay.paid_at else "Payment pending. ")
                + f"ACTUAL arrival {actual_start}; "
                f"ACTUAL departure {actual_end}. "
                "Inventory blocks previous and actual nights; following date is checkout. "
                "Check the website payment ledger."
            ),
        }
    )


def record_in_hostaway(*, request, stay_id, client=None):
    require_operations_access(request)
    if not settings.HOSTAWAY_LIVE_BOOKING_ENABLED or settings.HOSTAWAY_DIRECT_CHANNEL_ID != 2000:
        raise DayUseStayError("ربط إنشاء الحجوزات في هوستاوي غير مفعّل.")
    if client is None:
        with HostawayClient(max_get_attempts=1, timeout=10) as remote:
            return record_in_hostaway(request=request, stay_id=stay_id, client=remote)
    with transaction.atomic():
        stay = _locked(stay_id)
        operation = booking_state(stay)
        if operation.get("state") == "confirmed":
            if hostaway_first(stay):
                from .day_use08_communications import capture_event

                capture_event(stay.pk)
            return operation["reservation_id"]
        if operation:
            raise DayUseStayError("توجد عملية ربط سابقة؛ راجع نتيجتها ولا تنشئ حجزًا مكررًا.")
        if stay.environment != settings.HYPERPAY_ENVIRONMENT:
            raise DayUseStayError("بيئة الحجز لا تطابق بيئة التشغيل الحالية.")
        if not hostaway_first(stay) and (stay.status != "paid" or not stay.paid_at):
            raise DayUseStayError("وثّق استلام كامل مبلغ الحجز النهاري قبل تسجيله في هوستاوي.")
        if hostaway_first(stay) and stay.status not in {"open", "paid"}:
            raise DayUseStayError("حالة الحجز لا تسمح بالتأكيد في هوستاوي.")
        if hostaway_first(stay):
            from .day_use_billing import DayUseBillingForm

            if not settings.DAY_USE08_ENABLED:
                raise DayUseStayError(
                    "ربط التواصل للحجز النهاري غير مفعّل؛ لم ننشئ حجزًا في هوستاوي."
                )
            if not DayUseBillingForm(stay.billing_address).is_valid():
                raise DayUseStayError("أكمل عنوان الضيف الصحيح قبل تأكيد الحجز في هوستاوي.")
        if stay.attempts.filter(status__in=UNRESOLVED).exists():
            raise DayUseStayError("توجد محاولة دفع غير محسومة؛ تحقق منها أولًا.")
        expected = (
            stay.starts_at,
            stay.ends_at,
            stay.property_id,
            stay.guests,
            stay.amount_sar,
            stay.guest_name,
            stay.guest_phone,
            stay.guest_email,
            stay.guest_language,
            dict(stay.billing_address or {}),
        )
    evidence = verify_window(
        stay.property,
        stay.starts_at,
        stay.ends_at,
        guests=stay.guests,
        client=client,
        allow_started=True,
        allow_elapsed=not hostaway_first(stay),
    )
    with transaction.atomic():
        stay = _locked(stay_id)
        operation = booking_state(stay)
        if operation.get("state") == "confirmed":
            if hostaway_first(stay):
                from .day_use08_communications import capture_event

                capture_event(stay.pk)
            return operation["reservation_id"]
        eligible = stay.status == "paid" and stay.paid_at
        if hostaway_first(stay):
            eligible = stay.status in {"open", "paid"}
        if operation or not eligible:
            raise DayUseStayError("تغيرت حالة الحجز؛ افتح التفاصيل مجددًا.")
        if expected != (
            stay.starts_at,
            stay.ends_at,
            stay.property_id,
            stay.guests,
            stay.amount_sar,
            stay.guest_name,
            stay.guest_phone,
            stay.guest_email,
            stay.guest_language,
            dict(stay.billing_address or {}),
        ):
            raise DayUseStayError("تغيرت تفاصيل الإقامة؛ أعد التحقق قبل التسجيل.")
        if active_stays(
            stay.property, stay.starts_at, stay.ends_at + timedelta(hours=3), exclude=stay.pk
        ).exists():
            raise DayUseStayError("توجد إقامة نهارية أخرى متداخلة.")
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
        DayUseHostawayOwnership.objects.get_or_create(stay=stay)
    if not ownership.sync(stay.pk):
        with transaction.atomic():
            current = _locked(stay_id)
            _write_state(
                current, operation | {"state": "failed", "error": "ownership_unacknowledged"}
            )
            ownership.update(stay.pk, "failed")
        raise DayUseStayError("لم يُؤكد تسجيل ملكية المسار؛ لم يُرسل طلب إنشاء إلى هوستاوي.")
    # The durable claim commits before POST. Never retry an uncertain creation.
    created_id = None
    try:
        snapshot = client.create_reservation_with_price_details(payload).snapshot
        created_id = snapshot.reservation_id
        snapshot = client.get_reservation(created_id)
    except Exception as exc:
        with transaction.atomic():
            current = _locked(stay_id)
            _write_state(current, operation | {"state": "unknown", "reservation_id": created_id})
            ownership.update(stay_id, "unknown", created_id)
        raise DayUseStayError(
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
    ownership.update(stay_id, "confirmed" if matches else "unknown", snapshot.reservation_id)
    if not matches:
        raise DayUseStayError(
            "أنشئ سجل في هوستاوي لكن تفاصيله تحتاج مراجعة؛ حُفظ رقمه ومنعنا إنشاء سجل آخر."
        )
    if hostaway_first(current):
        from .day_use08_communications import capture_event

        with transaction.atomic():
            capture_event(current.pk)
    return snapshot.reservation_id


def cancel_linked_stay(stay_id, *, client=None):
    current = DayUseStay.objects.get(pk=stay_id)
    operation = booking_state(current)
    if operation.get("state") == "cancelled" and current.status == "cancelled":
        return current
    if operation.get("state") != "confirmed":
        raise DayUseStayError("نتيجة الربط غير محسومة؛ راجع هوستاوي قبل الإلغاء.")
    if client is None:
        try:
            with HostawayClient(max_get_attempts=1, timeout=10) as remote:
                return cancel_linked_stay(stay_id, client=remote)
        except HostawayError as exc:
            raise DayUseStayError("تعذر الاتصال بهوستاوي؛ لم نلغ الإقامة محليًا.") from exc
    with transaction.atomic():
        current = _locked(stay_id)
        operation = booking_state(current)
        if operation.get("state") != "confirmed":
            raise DayUseStayError("توجد عملية إلغاء أخرى؛ راجع حالة الحجز.")
        if current.attempts.filter(status__in=UNRESOLVED).exists():
            raise DayUseStayError("توجد محاولة دفع غير محسومة.")
        _write_state(current, operation | {"state": "cancelling"})
    try:
        remote = client.get_reservation(operation["reservation_id"])
        if (
            remote.listing_map_id != current.property.hostaway_listing_map_id
            or remote.source != PROVIDER
            or remote.check_in.isoformat() != operation["arrival_date"]
            or remote.check_out.isoformat() != operation["departure_date"]
        ):
            raise ValueError("day_use_booking_identity_mismatch")
        if remote.status.casefold() not in {"cancelled", "canceled"}:
            remote = client.cancel_day_use_reservation(operation["reservation_id"])
        if remote.status.casefold() not in {"cancelled", "canceled"}:
            raise ValueError("day_use_cancellation_not_confirmed")
    except Exception as exc:
        with transaction.atomic():
            current = _locked(stay_id)
            _write_state(current, operation | {"state": "cancel_unknown"})
        raise DayUseStayError("تعذر حسم إلغاء هوستاوي؛ لم نلغ الإقامة محليًا.") from exc
    with transaction.atomic():
        current = _locked(stay_id)
        _write_state(current, operation | {"state": "cancelled"})
        ownership.update(stay_id, "cancelled", operation["reservation_id"])
        current.status = "cancelled"
        current.save(update_fields=["status", "updated_at"])
        return current
