"""Read-only Hostaway checks and serialized internal day_use allocations."""

from datetime import datetime, time, timedelta
from decimal import ROUND_HALF_UP, Decimal
from zoneinfo import ZoneInfo

from django.conf import settings
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from apps.integrations.hostaway.client import HostawayClient
from apps.integrations.hostaway.reservation_validators import normalize_hostaway_reservation_status
from apps.properties.models import Property

from .models import DayUseStay, Reservation

ZONE = ZoneInfo("Asia/Riyadh")
UNRESOLVED = ("created", "pending", "unknown", "review")


class DayUseStayError(ValueError):
    pass


def property_zone(property_obj):
    name = property_obj.time_zone_name or (
        "Africa/Casablanca" if property_obj.country_code == "MA" else "Asia/Riyadh"
    )
    if name not in {"Asia/Riyadh", "Africa/Casablanca"}:
        raise DayUseStayError("المنطقة الزمنية للشقة غير معتمدة لهذا المسار.")
    return ZoneInfo(name)


def validate_window(starts_at, ends_at, *, zone=ZONE, allow_started=False, allow_elapsed=False):
    if timezone.is_naive(starts_at) or timezone.is_naive(ends_at):
        raise DayUseStayError("يلزم تحديد التاريخ والوقت والمنطقة الزمنية للشقة.")
    start, end = starts_at.astimezone(zone), ends_at.astimezone(zone)
    if start.date() != end.date() or not start.time() < end.time():
        raise DayUseStayError("اختر دخولًا وخروجًا في اليوم نفسه، والخروج بعد الدخول.")
    if (
        (starts_at < timezone.now() and not allow_started)
        or (ends_at <= timezone.now() and not allow_elapsed)
        or starts_at > timezone.now() + timedelta(days=14)
    ):
        raise DayUseStayError("اختر وقت دخول لم يبدأ بعد، خلال الأيام الأربعة عشر المقبلة.")


def active_stays(property_obj, start, end, *, exclude=None):
    zone = property_zone(property_obj)
    first = start.astimezone(zone).date() - timedelta(days=1)
    last = end.astimezone(zone).date() + timedelta(days=1)
    return (
        DayUseStay.objects.filter(
            property=property_obj,
            environment=settings.HYPERPAY_ENVIRONMENT,
            inventory_arrival_date__lt=last,
            inventory_departure_date__gt=first,
        )
        .exclude(pk=exclude)
        .filter(
            Q(status__in=["paid", "processing", "review"])
            | Q(status="open", expires_at__gt=timezone.now())
            | Q(attempts__status__in=UNRESOLVED)
            | Q(
                availability_evidence__hostaway_day_use__state__in=[
                    "creating",
                    "unknown",
                    "review",
                    "confirmed",
                    "cancelling",
                    "cancel_unknown",
                ]
            )
            | Q(ownership__state__in=["pending", "unknown", "confirmed"])
        )
    )


def overnight_conflicts(property_obj, check_in, check_out):
    """Normal site bookings must also respect day_use allocations and turnover."""
    return DayUseStay.objects.filter(
        pk__in=active_stays(
            property_obj,
            datetime.combine(check_in, time.min, property_zone(property_obj)),
            datetime.combine(check_out, time.min, property_zone(property_obj)),
        ).values("pk"),
        inventory_arrival_date__lt=check_out,
        inventory_departure_date__gt=check_in,
    ).exists()


def local_inflight_conflict(property_obj, start, end):
    """Include site checkouts and uncertain provider writes, not just remote bookings."""
    from apps.payments.models import PaymentAttempt

    zone = property_zone(property_obj)
    day = start.astimezone(zone).date()
    reservations = Reservation.objects.filter(
        property=property_obj,
        check_in__lt=day + timedelta(days=1),
        check_out__gt=day - timedelta(days=1),
        normalized_status__in=[
            "creating",
            "create_unknown",
            "create_pending",
            "ready_for_hostaway",
        ],
    )
    attempts = PaymentAttempt.objects.filter(
        booking_intent__property=property_obj,
        booking_intent__check_in__lt=day + timedelta(days=1),
        booking_intent__check_out__gt=day - timedelta(days=1),
        status__in=[*UNRESOLVED, "succeeded"],
    ).select_related("booking_intent")
    intervals = [(r.check_in, r.check_out) for r in reservations]
    intervals.extend((a.booking_intent.check_in, a.booking_intent.check_out) for a in attempts)
    for arrival, departure in intervals:
        if arrival < day + timedelta(days=1) and departure > day - timedelta(days=1):
            return True
    return False


def verify_window(
    property_obj,
    starts_at,
    ends_at,
    *,
    guests=1,
    client=None,
    allow_started=False,
    allow_elapsed=False,
    linked_stay=None,
):
    """No cache, quote, payment, reservation write, or phone-based exclusion."""
    zone = property_zone(property_obj)
    validate_window(
        starts_at, ends_at, zone=zone, allow_started=allow_started, allow_elapsed=allow_elapsed
    )
    if (
        not property_obj.is_visible
        or not property_obj.hostaway_is_active
        or not property_obj.hostaway_listing_map_id
        or guests < 1
        or (property_obj.person_capacity and guests > property_obj.person_capacity)
    ):
        raise DayUseStayError("الوحدة أو ربطها أو سعتها غير متحقق لهذا المسار.")
    if client is None:
        with HostawayClient(max_get_attempts=1, timeout=5) as remote:
            return verify_window(
                property_obj,
                starts_at,
                ends_at,
                guests=guests,
                client=remote,
                allow_started=allow_started,
                allow_elapsed=allow_elapsed,
                linked_stay=linked_stay,
            )
    if local_inflight_conflict(property_obj, starts_at, ends_at):
        raise DayUseStayError("توجد محاولة حجز أو دفع أخرى متداخلة غير محسومة.")
    day = starts_at.astimezone(zone).date()
    previous_day = day - timedelta(days=1)
    calendar = client.get_listing_calendar(
        property_obj.hostaway_listing_id,
        start_date=previous_day,
        end_date=day + timedelta(days=1),
        include_resources=False,
    )
    days = {row.date: row for row in calendar.days}
    if any(d not in days for d in (previous_day, day, day + timedelta(days=1))):
        raise DayUseStayError("تعذر التحقق من تقويم الليلة السابقة واليوم المطلوب.")
    for row in (days[previous_day], days[day]):
        # Zero/one unit counters are also returned for individual apartments.
        # Only actual multi-unit stock requires a physical-unit assignment.
        counts = (
            row.count_available_units,
            row.available_units_to_sell,
            row.desired_units_to_sell,
            row.count_reserved_units,
            row.count_pending_units,
            row.count_blocked_units,
        )
        stock = sum(
            value or 0
            for value in (
                row.count_available_units,
                row.count_reserved_units,
                row.count_pending_units,
                row.count_blocked_units,
            )
        )
        if stock > 1 or any(value is not None and value > 1 for value in counts):
            raise DayUseStayError("التقويم مجمع لعدة وحدات؛ يلزم تعيين الشقة الفعلية قبل الدفع.")
        if row.count_blocked_units or row.status.casefold() in {"blocked", "closed", "maintenance"}:
            raise DayUseStayError("يوجد حجب أو إغلاق يحتاج مراجعة الإدارة.")

    if linked_stay is None:
        for row in (days[previous_day], days[day]):
            if row.is_available is not True or row.count_reserved_units or row.count_pending_units:
                raise DayUseStayError("ليلتَا الحجب غير متاحتين بالكامل.")
            if (row.minimum_stay and row.minimum_stay > 2) or (
                row.maximum_stay and row.maximum_stay < 2
            ):
                raise DayUseStayError("قيود مدة الحجز في هوستاوي لا تسمح بليلتَي الحجب.")
        if (
            days[previous_day].closed_on_arrival
            or days[day + timedelta(days=1)].closed_on_departure
        ):
            raise DayUseStayError("يوجد قيد وصول أو مغادرة على فترة الحجب.")
    from .morning_stays import active_stays as morning_active

    if morning_active(
        property_obj,
        datetime.combine(previous_day, time(15), zone),
        datetime.combine(day + timedelta(days=1), time(12), zone),
    ).exists():
        raise DayUseStayError("يوجد طلب صباحي يتعارض مع ليلتَي الحجب.")
    cursor, seen, linked_seen = None, set(), False
    for _ in range(10):
        rows = client.guest_stay_page(previous_day, day + timedelta(days=1), after_id=cursor)
        for row in rows:
            identifier = row.get("id")
            if type(identifier) is not int or identifier <= 0 or identifier in seen:
                raise DayUseStayError("تغطية الحجوزات غير مكتملة.")
            seen.add(identifier)
            listing_map_id = row.get("listingMapId")
            if type(listing_map_id) is not int or listing_map_id <= 0:
                raise DayUseStayError("هوية الشقة في بيانات الحجوزات غير متحققة.")
            if listing_map_id != property_obj.hostaway_listing_map_id:
                continue
            status = normalize_hostaway_reservation_status(str(row.get("status", "")))
            if linked_stay is not None:
                from .day_use_hostaway import PROVIDER, booking_state

                own = booking_state(linked_stay)
                if identifier == own.get("reservation_id"):
                    if (
                        own.get("state") != "confirmed"
                        or row.get("source") != PROVIDER
                        or row.get("arrivalDate") != own.get("arrival_date")
                        or row.get("departureDate") != own.get("departure_date")
                        or status not in {"confirmed", "modified"}
                    ):
                        raise DayUseStayError("تغير حجز هوستاوي المرتبط؛ راجع الإدارة قبل الدفع.")
                    linked_seen = True
                    continue
            if status in {"cancelled", "declined", "expired", "inquiry"}:
                continue
            if status not in {"confirmed", "modified", "awaiting_payment", "pending"}:
                raise DayUseStayError("توجد حالة حجز غير محسومة للوحدة.")
            try:
                arrival = datetime.fromisoformat(row["arrivalDate"]).date()
                departure = datetime.fromisoformat(row["departureDate"]).date()
                if departure <= arrival:
                    raise ValueError
            except (ValueError, TypeError, KeyError) as exc:
                raise DayUseStayError("تواريخ إشغال الوحدة غير متحققة.") from exc
            if arrival < day + timedelta(days=1) and departure > previous_day:
                raise DayUseStayError("يوجد حجز يتعارض مع ليلتَي الحجب؛ اختر شقة أخرى.")
        if len(rows) < 100:
            break
        cursor = rows[-1]["id"]
    else:
        raise DayUseStayError("تعذر إكمال صفحات الحجوزات؛ لم يُثبت التوفر.")
    if linked_stay is not None and not linked_seen:
        raise DayUseStayError("لم يظهر حجز هوستاوي المرتبط ضمن التوفر؛ راجع الإدارة قبل الدفع.")
    if linked_stay is not None:
        from .day_use_hostaway import PROVIDER, booking_state

        own = booking_state(linked_stay)
        remote = client.get_reservation(own["reservation_id"])
        if (
            remote.reservation_id != own["reservation_id"]
            or remote.listing_map_id != property_obj.hostaway_listing_map_id
            or remote.source != PROVIDER
            or remote.check_in != linked_stay.inventory_arrival_date
            or remote.check_out != linked_stay.inventory_departure_date
            or remote.total_price != linked_stay.amount_sar
            or remote.currency != "SAR"
            or remote.status.casefold() not in {"new", "confirmed", "modified"}
        ):
            raise DayUseStayError("تغيرت هوية حجز هوستاوي أو قيمته؛ يلزم مراجعة الإدارة.")
    pricing = (
        None
        if linked_stay is not None
        else price_snapshot(property_obj, starts_at, ends_at, days[day].price, client=client)
    )
    return {
        "pricing": pricing,
        "checked_at": timezone.now().isoformat(),
        "source": "hostaway_read_only",
        "previous_night": previous_day.isoformat(),
        "reservation_rows_checked": len(seen),
        "readiness_policy": "owner_scheduled_departure_and_no_overlapping_stay",
    }


def calculate_price(base, starts_at, ends_at, *, source_rate_per_sar=Decimal("1")):
    base, rate = Decimal(str(base)), Decimal(str(source_rate_per_sar))
    if not base.is_finite() or base <= 0 or not rate.is_finite() or rate <= 0:
        raise DayUseStayError("السعر الأساسي أو سعر الصرف غير صالح.")
    elapsed = ends_at - starts_at
    hours = Decimal(elapsed.days * 86400 + elapsed.seconds) / Decimal(3600)
    if hours <= 0:
        raise DayUseStayError("مدة الإقامة غير صالحة.")
    amount = (base / Decimal(21) * hours * Decimal("1.5") / rate).quantize(
        Decimal("0.01"), rounding=ROUND_HALF_UP
    )
    if not Decimal("0") < amount <= Decimal("9999999"):
        raise DayUseStayError("المبلغ المحسوب خارج الحد المتاح للدفع.")
    return amount, hours


def price_snapshot(property_obj, starts_at, ends_at, base, *, client):
    from apps.integrations.hostaway.availability_validators import resolve_hostaway_price_currency
    from apps.payments.currency import CurrencyError, CurrencyService

    if base is None:
        raise DayUseStayError("سعر اليوم الأساسي غير متاح في التقويم المباشر.")
    record = client.get_listing_currency_record(property_obj.hostaway_listing_id, bypass_cache=True)
    currency = resolve_hostaway_price_currency(
        listing_id=property_obj.hostaway_listing_id,
        price_details_response={"result": {}},
        listing_response=record,
        currency_override=property_obj.price_currency_override or "",
    )
    rate, fx = Decimal("1"), None
    if currency != "SAR":
        try:
            with CurrencyService() as service:
                rates = service.get_rates()
            rate, fx = rates.rate(currency), rates.to_cache_value()
        except CurrencyError as exc:
            raise DayUseStayError("تعذر التحقق من سعر الصرف؛ لم نعتمد المبلغ.") from exc
    amount, hours = calculate_price(base, starts_at, ends_at, source_rate_per_sar=rate)
    return {
        "version": 1,
        "source": "hostaway_calendar",
        "base_date": starts_at.astimezone(property_zone(property_obj)).date().isoformat(),
        "base_price": str(base),
        "source_currency": currency,
        "divisor": "21",
        "multiplier": "1.5",
        "hours": str(hours),
        "source_rate_per_sar": str(rate),
        "fx": fx,
        "amount_sar": str(amount),
        "payment_currency": "SAR",
        "checked_at": timezone.now().isoformat(),
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
    guest_language="ar",
    billing_address=None,
    client=None,
    expected_pricing=None,
):
    zone = property_zone(property_obj)
    day = starts_at.astimezone(zone).date()
    values = dict(
        property_id=property_obj.pk,
        starts_at=starts_at,
        ends_at=ends_at,
        guest_name=guest_name,
        guest_phone=guest_phone,
        guest_email=guest_email,
        guest_language=guest_language,
        guests=guests,
        billing_address=billing_address or {},
        time_zone=zone.key,
        environment=settings.HYPERPAY_ENVIRONMENT,
        inventory_arrival_date=day - timedelta(days=1),
        inventory_departure_date=day + timedelta(days=1),
    )
    existing = DayUseStay.objects.filter(request_id=request_id).first()
    if existing:
        if existing.created_by_id != actor.pk or any(
            getattr(existing, k) != v for k, v in values.items()
        ):
            raise DayUseStayError("تكرر معرف الطلب ببيانات مختلفة؛ لم ننشئ حجزًا آخر.")
        return existing
    evidence = verify_window(property_obj, starts_at, ends_at, guests=guests, client=client)
    if expected_pricing is not None and any(
        evidence["pricing"].get(k) != expected_pricing.get(k)
        for k in (
            "base_date",
            "base_price",
            "source_currency",
            "hours",
            "divisor",
            "multiplier",
            "source_rate_per_sar",
            "amount_sar",
        )
    ):
        raise DayUseStayError("تغير السعر منذ الفحص؛ راجع السعر الجديد قبل إنشاء الحجز.")
    evidence["creation_mode"] = "hostaway_first_v2"
    with transaction.atomic():
        Property.objects.select_for_update().get(pk=property_obj.pk)
        existing = DayUseStay.objects.filter(request_id=request_id).first()
        if existing:
            if existing.created_by_id != actor.pk or any(
                getattr(existing, k) != v for k, v in values.items()
            ):
                raise DayUseStayError("تغيرت بيانات الطلب؛ أعد فتح النموذج.")
            return existing
        if active_stays(property_obj, starts_at, ends_at).exists() or local_inflight_conflict(
            property_obj, starts_at, ends_at
        ):
            raise DayUseStayError("بدأ طلب آخر متداخل؛ أعد فحص التوفر.")
        from .morning_stays import active_stays as morning_active

        if morning_active(
            property_obj,
            datetime.combine(day - timedelta(days=1), time(15), zone),
            datetime.combine(day + timedelta(days=1), time(12), zone),
        ).exists():
            raise DayUseStayError("بدأ طلب صباحي يتعارض مع الحجب؛ أعد الفحص.")
        stay = DayUseStay(
            **values,
            request_id=request_id,
            created_by=actor,
            amount_sar=Decimal(evidence["pricing"]["amount_sar"]),
            pricing_snapshot=evidence["pricing"],
            expires_at=min(timezone.now() + timedelta(minutes=5), starts_at),
            availability_checked_at=timezone.now(),
            availability_evidence=evidence,
        )
        stay.full_clean()
        stay.save()
        return stay
