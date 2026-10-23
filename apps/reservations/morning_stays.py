"""Read-only Hostaway checks and serialized internal morning allocations."""

from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from django.conf import settings
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from apps.integrations.hostaway.client import HostawayClient
from apps.integrations.hostaway.reservation_validators import normalize_hostaway_reservation_status
from apps.properties.models import Property

from .models import MorningStay, Reservation
from .services.availability import resolve_day_inventory

ZONE = ZoneInfo("Asia/Riyadh")
UNRESOLVED = ("created", "pending", "unknown", "review")


class MorningStayError(ValueError):
    pass


def property_zone(property_obj):
    name = property_obj.time_zone_name or (
        "Africa/Casablanca" if property_obj.country_code == "MA" else "Asia/Riyadh"
    )
    if name not in {"Asia/Riyadh", "Africa/Casablanca"}:
        raise MorningStayError("المنطقة الزمنية للشقة غير معتمدة لهذا المسار.")
    return ZoneInfo(name)


def validate_window(starts_at, ends_at, *, zone=ZONE, allow_started=False):
    if timezone.is_naive(starts_at) or timezone.is_naive(ends_at):
        raise MorningStayError("يلزم تحديد التاريخ والوقت والمنطقة الزمنية للشقة.")
    start, end = starts_at.astimezone(zone), ends_at.astimezone(zone)
    if start.date() != end.date() or not time.min < start.time() < end.time() < time(12):
        raise MorningStayError("الدخول بعد منتصف الليل والخروج قبل ١٢ ظهرًا في اليوم نفسه.")
    if (
        (starts_at < timezone.now() and not allow_started)
        or ends_at <= timezone.now()
        or starts_at > timezone.now() + timedelta(days=14)
    ):
        raise MorningStayError("اختر وقت دخول لم يبدأ بعد، خلال الأيام الأربعة عشر المقبلة.")


def active_stays(property_obj, start, end, *, exclude=None):
    return (
        MorningStay.objects.filter(
            property=property_obj,
            environment=settings.HYPERPAY_ENVIRONMENT,
            starts_at__lt=end,
            ends_at__gt=start - timedelta(hours=3),
        )
        .exclude(pk=exclude)
        .filter(
            Q(status__in=["paid", "processing", "review"])
            | Q(status="open", expires_at__gt=timezone.now())
            | Q(attempts__status__in=UNRESOLVED)
        )
    )


def overnight_conflicts(property_obj, check_in, check_out):
    """Normal site bookings must also respect morning allocations and turnover."""
    zone = ZoneInfo(
        property_obj.time_zone_name
        or ("Africa/Casablanca" if property_obj.country_code == "MA" else "Asia/Riyadh")
    )
    in_hour = property_obj.display_check_in_hour
    out_hour = property_obj.display_check_out_hour
    start = datetime.combine(check_in, time.min, zone) + timedelta(
        hours=15 if in_hour is None else in_hour
    )
    end = datetime.combine(check_out, time.min, zone) + timedelta(
        hours=12 if out_hour is None else out_hour
    )
    return active_stays(property_obj, start, end).exists()


def local_inflight_conflict(property_obj, start, end):
    """Include site checkouts and uncertain provider writes, not just remote bookings."""
    from apps.payments.models import PaymentAttempt

    zone = property_zone(property_obj)
    day = start.astimezone(zone).date()
    reservations = Reservation.objects.filter(
        property=property_obj,
        check_in__lte=day,
        check_out__gte=day,
        normalized_status__in=[
            "creating",
            "create_unknown",
            "create_pending",
            "ready_for_hostaway",
        ],
    )
    attempts = PaymentAttempt.objects.filter(
        booking_intent__property=property_obj,
        booking_intent__check_in__lte=day,
        booking_intent__check_out__gte=day,
        status__in=[*UNRESOLVED, "succeeded"],
    ).select_related("booking_intent")
    intervals = [(r.check_in, r.check_out) for r in reservations]
    intervals.extend((a.booking_intent.check_in, a.booking_intent.check_out) for a in attempts)
    for arrival, departure in intervals:
        a = datetime.combine(arrival, time(15), zone)
        b = datetime.combine(departure, time(12), zone)
        if a < end + timedelta(hours=3) and b > start:
            return True
    return False


def verify_window(property_obj, starts_at, ends_at, *, guests=1, client=None, allow_started=False):
    """No cache, quote, payment, reservation write, or phone-based exclusion."""
    zone = property_zone(property_obj)
    validate_window(starts_at, ends_at, zone=zone, allow_started=allow_started)
    if (
        not property_obj.is_visible
        or not property_obj.hostaway_is_active
        or not property_obj.hostaway_listing_map_id
        or guests < 1
        or (property_obj.person_capacity and guests > property_obj.person_capacity)
    ):
        raise MorningStayError("الوحدة أو ربطها أو سعتها غير متحقق لهذا المسار.")
    if client is None:
        with HostawayClient(max_get_attempts=1, timeout=5) as remote:
            return verify_window(
                property_obj,
                starts_at,
                ends_at,
                guests=guests,
                client=remote,
                allow_started=allow_started,
            )
    if local_inflight_conflict(property_obj, starts_at, ends_at):
        raise MorningStayError("توجد محاولة حجز أو دفع أخرى متداخلة غير محسومة.")
    day = starts_at.astimezone(zone).date()
    previous_day = day - timedelta(days=1)
    calendar = client.get_listing_calendar(
        property_obj.hostaway_listing_id,
        start_date=previous_day,
        end_date=day + timedelta(days=1),
        include_resources=False,
    )
    days = {row.date: row for row in calendar.days}
    if previous_day not in days or day not in days:
        raise MorningStayError("تعذر التحقق من تقويم الليلة السابقة واليوم المطلوب.")
    previous = resolve_day_inventory(days[previous_day])
    current = resolve_day_inventory(days[day])
    if previous.is_multi_unit or current.is_multi_unit:
        raise MorningStayError("التقويم مجمع لعدة وحدات؛ يلزم تعيين الشقة الفعلية قبل الدفع.")
    if previous.has_conflict or current.has_conflict:
        raise MorningStayError("إشارات التوفر متعارضة؛ يلزم مراجعة الإدارة.")
    for row in (days[previous_day], days[day]):
        if row.count_blocked_units or row.status.casefold() in {"blocked", "closed", "maintenance"}:
            raise MorningStayError("يوجد حجب أو إغلاق يحتاج مراجعة الإدارة.")

    cursor, seen, future_night_booked = None, set(), False
    for _ in range(10):
        rows = client.guest_stay_page(previous_day, day, after_id=cursor)
        for row in rows:
            identifier = row.get("id")
            if type(identifier) is not int or identifier <= 0 or identifier in seen:
                raise MorningStayError("تغطية الحجوزات غير مكتملة.")
            seen.add(identifier)
            if row.get("listingMapId") != property_obj.hostaway_listing_map_id:
                continue
            status = normalize_hostaway_reservation_status(str(row.get("status", "")))
            if status in {"cancelled", "declined", "expired", "inquiry"}:
                continue
            if status not in {"confirmed", "modified", "awaiting_payment", "pending"}:
                raise MorningStayError("توجد حالة حجز غير محسومة للوحدة.")
            try:
                arrival = datetime.fromisoformat(row["arrivalDate"]).date()
                departure = datetime.fromisoformat(row["departureDate"]).date()
                if departure <= arrival:
                    raise ValueError
                in_hour = row.get("checkInTime")
                out_hour = row.get("checkOutTime")
                in_hour = in_hour if in_hour is not None else property_obj.display_check_in_hour
                out_hour = out_hour if out_hour is not None else property_obj.display_check_out_hour
                in_hour = 15 if in_hour is None else in_hour
                out_hour = 12 if out_hour is None else out_hour
                if any(type(h) is not int or not 0 <= h <= 24 for h in (in_hour, out_hour)):
                    raise ValueError
                start = datetime.combine(arrival, time.min, zone) + timedelta(hours=in_hour)
                end = datetime.combine(departure, time.min, zone) + timedelta(hours=out_hour)
            except (ValueError, TypeError, KeyError) as exc:
                raise MorningStayError("أوقات إشغال الوحدة غير متحققة.") from exc
            # An afternoon arrival is fine when the three-hour turnover fits.
            if start < ends_at + timedelta(hours=3) and end > starts_at:
                raise MorningStayError("يوجد إشغال أو وصول مبكر يتعارض مع الفترة أو تجهيز الوحدة.")
            if arrival == day and start >= ends_at + timedelta(hours=3):
                future_night_booked = True
        if len(rows) < 100:
            break
        cursor = rows[-1]["id"]
    else:
        raise MorningStayError("تعذر إكمال صفحات الحجوزات؛ لم يُثبت التوفر.")
    if not previous.is_available:
        raise MorningStayError("الليلة السابقة محجوبة أو غير متاحة؛ لا يمكن تأكيد الصباح.")
    if not current.is_available and not future_night_booked:
        raise MorningStayError("إغلاق اليوم غير مفسر بحجز يبدأ بعد فترة الصباح؛ يلزم مراجعة.")
    return {
        "checked_at": timezone.now().isoformat(),
        "source": "hostaway_read_only",
        "previous_night": previous_day.isoformat(),
        "reservation_rows_checked": len(seen),
        "readiness_policy": "owner_immediate_turnover_no_overlapping_stay",
    }


def create_stay(
    *,
    actor,
    request_id,
    property_obj,
    starts_at,
    ends_at,
    guest_name,
    guest_phone,
    guest_email,
    guests,
    amount_sar,
    guest_language="ar",
    client=None,
):
    existing = MorningStay.objects.filter(request_id=request_id).first()
    values = dict(
        property_id=property_obj.pk,
        starts_at=starts_at,
        ends_at=ends_at,
        guest_name=guest_name,
        guest_phone=guest_phone,
        guest_email=guest_email,
        guest_language=guest_language,
        guests=guests,
        amount_sar=amount_sar,
        time_zone=property_zone(property_obj).key,
        environment=settings.HYPERPAY_ENVIRONMENT,
    )
    if existing:
        if any(getattr(existing, k) != v for k, v in values.items()):
            raise MorningStayError("تكرر معرف الطلب ببيانات مختلفة؛ أعد فتح النموذج.")
        return existing
    evidence = verify_window(property_obj, starts_at, ends_at, guests=guests, client=client)
    with transaction.atomic():
        Property.objects.select_for_update().get(pk=property_obj.pk)
        existing = MorningStay.objects.filter(request_id=request_id).first()
        if existing:
            if any(getattr(existing, k) != v for k, v in values.items()):
                raise MorningStayError("تكرر معرف الطلب ببيانات مختلفة.")
            return existing
        if active_stays(property_obj, starts_at, ends_at + timedelta(hours=3)).exists():
            raise MorningStayError("يوجد طلب صباحي آخر متداخل للوحدة؛ لم يُنشأ طلب مكرر.")
        if local_inflight_conflict(property_obj, starts_at, ends_at):
            raise MorningStayError("بدأ طلب حجز آخر للوحدة؛ أعد فحص التوفر.")
        stay = MorningStay(
            **values,
            request_id=request_id,
            created_by=actor,
            expires_at=min(timezone.now() + timedelta(minutes=5), starts_at),
            availability_checked_at=timezone.now(),
            availability_evidence=evidence,
        )
        stay.full_clean()
        stay.save()
        return stay
