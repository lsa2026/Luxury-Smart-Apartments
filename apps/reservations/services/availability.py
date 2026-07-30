"""Conservative, read-only Hostaway availability and price verification."""

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any, Protocol

from django.conf import settings
from django.core.cache import cache as default_cache
from django.utils import timezone

from apps.integrations.hostaway.availability_validators import (
    CalendarDay,
    CalendarDocument,
    PriceComponent,
    PriceQuote,
)
from apps.integrations.hostaway.client import HostawayClient
from apps.integrations.hostaway.exceptions import (
    HostawayAvailabilityError,
    HostawayError,
)
from apps.properties.models import Property

logger = logging.getLogger(__name__)

AVAILABLE = "available"
INVALID_DATES = "invalid_dates"
PAST_CHECK_IN = "past_check_in"
CAPACITY_EXCEEDED = "capacity_exceeded"
PROPERTY_UNAVAILABLE = "property_unavailable"
CALENDAR_INCOMPLETE = "calendar_incomplete"
UNAVAILABLE_DATES = "unavailable_dates"
CLOSED_ON_ARRIVAL = "closed_on_arrival"
CLOSED_ON_DEPARTURE = "closed_on_departure"
MINIMUM_STAY_NOT_MET = "minimum_stay_not_met"
MAXIMUM_STAY_EXCEEDED = "maximum_stay_exceeded"
PRICING_UNAVAILABLE = "pricing_unavailable"
HOSTAWAY_TEMPORARILY_UNAVAILABLE = "hostaway_temporarily_unavailable"

MESSAGES = {
    AVAILABLE: ("الفترة متاحة.", "The stay is available."),
    INVALID_DATES: ("تواريخ الإقامة غير صالحة.", "The stay dates are invalid."),
    PAST_CHECK_IN: ("لا يمكن أن يكون الوصول في الماضي.", "Check-in cannot be in the past."),
    CAPACITY_EXCEEDED: (
        "عدد الضيوف يتجاوز سعة الوحدة.",
        "The guest count exceeds the property capacity.",
    ),
    PROPERTY_UNAVAILABLE: (
        "هذه الوحدة غير متاحة للحجز حاليًا.",
        "This property is not currently available to book.",
    ),
    CALENDAR_INCOMPLETE: (
        "تعذر تأكيد جميع أيام الفترة المطلوبة.",
        "Not all requested calendar days could be confirmed.",
    ),
    UNAVAILABLE_DATES: (
        "الفترة المطلوبة غير متاحة.",
        "The requested dates are unavailable.",
    ),
    CLOSED_ON_ARRIVAL: (
        "الوصول غير مسموح في التاريخ المحدد.",
        "Arrival is not allowed on the selected date.",
    ),
    CLOSED_ON_DEPARTURE: (
        "المغادرة غير مسموحة في التاريخ المحدد.",
        "Departure is not allowed on the selected date.",
    ),
    MINIMUM_STAY_NOT_MET: (
        "الفترة أقصر من الحد الأدنى للإقامة.",
        "The stay is shorter than the minimum stay.",
    ),
    MAXIMUM_STAY_EXCEEDED: (
        "الفترة تتجاوز الحد الأقصى للإقامة.",
        "The stay exceeds the maximum stay.",
    ),
    PRICING_UNAVAILABLE: (
        "تعذر حساب السعر لهذه الفترة.",
        "A price could not be calculated for this stay.",
    ),
    HOSTAWAY_TEMPORARILY_UNAVAILABLE: (
        "تعذر التحقق الآن. يرجى المحاولة لاحقًا.",
        "Availability cannot be checked right now. Please try again later.",
    ),
}


class CacheBackend(Protocol):
    def get(self, key: str, default: Any = None) -> Any: ...

    def set(self, key: str, value: Any, timeout: int | None = None) -> None: ...


@dataclass(frozen=True, slots=True)
class AvailabilityRequest:
    property: Property
    check_in: date
    check_out: date
    guests: int


@dataclass(frozen=True, slots=True)
class AvailabilityResult:
    is_available: bool
    reason_code: str
    user_message_ar: str
    user_message_en: str
    nights: int
    quote: PriceQuote | None = None
    calendar_duration_ms: int = 0
    price_duration_ms: int = 0
    calendar_cache_hit: bool = False
    price_cache_hit: bool = False
    calendar_document: CalendarDocument | None = None


@dataclass(frozen=True, slots=True)
class CalendarFetch:
    document: CalendarDocument
    duration_ms: int
    cache_hit: bool


class AvailabilityService:
    """Validate a stay conservatively, then request Hostaway's authoritative price."""

    def __init__(
        self,
        *,
        client: HostawayClient | None = None,
        cache_backend: CacheBackend | None = None,
        timer: Callable[[], float] = time.perf_counter,
    ) -> None:
        self.client = client or HostawayClient()
        self._owns_client = client is None
        self.cache = cache_backend or default_cache
        self.timer = timer

    def close(self) -> None:
        if self._owns_client:
            self.client.close()

    def __enter__(self) -> "AvailabilityService":
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    def check(
        self,
        request: AvailabilityRequest,
        *,
        bypass_cache: bool = False,
    ) -> AvailabilityResult:
        """Check local rules, calendar restrictions, and finally priceDetails v2."""
        invalid = self._validate_request(request)
        if invalid is not None:
            return invalid

        try:
            calendar = self.fetch_calendar(
                property_obj=request.property,
                start_date=request.check_in,
                end_date=request.check_out,
                bypass_cache=bypass_cache,
            )
        except (HostawayError, ValueError):
            logger.exception(
                "Hostaway calendar verification failed for listing_id=%s",
                request.property.hostaway_listing_id,
            )
            return self._result(
                HOSTAWAY_TEMPORARILY_UNAVAILABLE,
                nights=(request.check_out - request.check_in).days,
            )

        calendar_reason = evaluate_calendar(
            calendar.document.days,
            check_in=request.check_in,
            check_out=request.check_out,
        )
        if calendar_reason != AVAILABLE:
            return self._result(
                calendar_reason,
                nights=(request.check_out - request.check_in).days,
                calendar=calendar,
            )

        try:
            quote, price_duration_ms, price_cache_hit = self._fetch_price(
                request,
                bypass_cache=bypass_cache,
            )
        except HostawayAvailabilityError:
            return self._result(
                PRICING_UNAVAILABLE,
                nights=(request.check_out - request.check_in).days,
                calendar=calendar,
            )
        except (HostawayError, ValueError):
            logger.exception(
                "Hostaway price verification failed for listing_id=%s",
                request.property.hostaway_listing_id,
            )
            return self._result(
                HOSTAWAY_TEMPORARILY_UNAVAILABLE,
                nights=(request.check_out - request.check_in).days,
                calendar=calendar,
            )

        if (
            request.property.currency_code
            and quote.currency != request.property.currency_code.upper()
        ):
            logger.warning(
                "Hostaway quote currency differs from local listing currency: listing_id=%s",
                request.property.hostaway_listing_id,
            )

        message_ar, message_en = MESSAGES[AVAILABLE]
        return AvailabilityResult(
            is_available=True,
            reason_code=AVAILABLE,
            user_message_ar=message_ar,
            user_message_en=message_en,
            nights=quote.nights,
            quote=quote,
            calendar_duration_ms=calendar.duration_ms,
            price_duration_ms=price_duration_ms,
            calendar_cache_hit=calendar.cache_hit,
            price_cache_hit=price_cache_hit,
            calendar_document=calendar.document,
        )

    def find_first_available(
        self,
        *,
        property_obj: Property,
        start_date: date,
        scan_days: int,
        stay_nights: int,
        guests: int,
        bypass_cache: bool = False,
    ) -> tuple[AvailabilityRequest | None, AvailabilityResult, CalendarFetch]:
        """Scan one fetched calendar and price at most one qualifying stay."""
        if scan_days < stay_nights or scan_days > 366 or stay_nights < 1:
            raise ValueError("scan_days and stay_nights define an invalid window.")
        probe = AvailabilityRequest(
            property=property_obj,
            check_in=start_date,
            check_out=start_date + timedelta(days=stay_nights),
            guests=guests,
        )
        invalid = self._validate_request(probe)
        if invalid is not None:
            empty_calendar = CalendarFetch(
                CalendarDocument((), frozenset(), ()),
                0,
                False,
            )
            return None, invalid, empty_calendar
        end_date = start_date + timedelta(days=scan_days)
        calendar = self.fetch_calendar(
            property_obj=property_obj,
            start_date=start_date,
            end_date=end_date,
            bypass_cache=bypass_cache,
        )
        latest_start = end_date - timedelta(days=stay_nights)
        candidate = start_date
        while candidate <= latest_start:
            candidate_end = candidate + timedelta(days=stay_nights)
            if (
                evaluate_calendar(
                    calendar.document.days,
                    check_in=candidate,
                    check_out=candidate_end,
                )
                == AVAILABLE
            ):
                request = AvailabilityRequest(
                    property=property_obj,
                    check_in=candidate,
                    check_out=candidate_end,
                    guests=guests,
                )
                result = self._check_with_calendar(
                    request,
                    calendar,
                    bypass_cache=bypass_cache,
                )
                return request, result, calendar
            candidate += timedelta(days=1)

        result = self._result(
            UNAVAILABLE_DATES,
            nights=stay_nights,
            calendar=calendar,
        )
        return None, result, calendar

    def fetch_calendar(
        self,
        *,
        property_obj: Property,
        start_date: date,
        end_date: date,
        bypass_cache: bool,
    ) -> CalendarFetch:
        cache_key = (
            f"hostaway:calendar:v1:{property_obj.hostaway_listing_id}:"
            f"{start_date.isoformat()}:{end_date.isoformat()}:resources0"
        )
        if not bypass_cache:
            cached = self.cache.get(cache_key)
            if isinstance(cached, CalendarDocument):
                return CalendarFetch(cached, 0, True)

        started = self.timer()
        document = self.client.get_listing_calendar(
            property_obj.hostaway_listing_id,
            start_date=start_date,
            end_date=end_date,
            include_resources=False,
        )
        duration_ms = max(0, round((self.timer() - started) * 1000))
        self.cache.set(
            cache_key,
            document,
            timeout=settings.HOSTAWAY_CALENDAR_CACHE_TTL,
        )
        return CalendarFetch(document, duration_ms, False)

    def _check_with_calendar(
        self,
        request: AvailabilityRequest,
        calendar: CalendarFetch,
        *,
        bypass_cache: bool,
    ) -> AvailabilityResult:
        invalid = self._validate_request(request)
        if invalid is not None:
            return invalid
        reason = evaluate_calendar(
            calendar.document.days,
            check_in=request.check_in,
            check_out=request.check_out,
        )
        if reason != AVAILABLE:
            return self._result(
                reason,
                nights=(request.check_out - request.check_in).days,
                calendar=calendar,
            )
        try:
            quote, duration_ms, cache_hit = self._fetch_price(
                request,
                bypass_cache=bypass_cache,
            )
        except HostawayAvailabilityError:
            return self._result(
                PRICING_UNAVAILABLE,
                nights=(request.check_out - request.check_in).days,
                calendar=calendar,
            )
        except HostawayError:
            return self._result(
                HOSTAWAY_TEMPORARILY_UNAVAILABLE,
                nights=(request.check_out - request.check_in).days,
                calendar=calendar,
            )
        message_ar, message_en = MESSAGES[AVAILABLE]
        return AvailabilityResult(
            is_available=True,
            reason_code=AVAILABLE,
            user_message_ar=message_ar,
            user_message_en=message_en,
            nights=(request.check_out - request.check_in).days,
            quote=quote,
            calendar_duration_ms=calendar.duration_ms,
            price_duration_ms=duration_ms,
            calendar_cache_hit=calendar.cache_hit,
            price_cache_hit=cache_hit,
            calendar_document=calendar.document,
        )

    def _fetch_price(
        self,
        request: AvailabilityRequest,
        *,
        bypass_cache: bool,
    ) -> tuple[PriceQuote, int, bool]:
        cache_key = (
            f"hostaway:price:v2:{request.property.hostaway_listing_id}:"
            f"{request.check_in.isoformat()}:{request.check_out.isoformat()}:"
            f"{request.guests}"
        )
        if not bypass_cache:
            cached = self.cache.get(cache_key)
            if isinstance(cached, PriceQuote):
                return cached, 0, True

        started = self.timer()
        quote = self.client.calculate_price(
            request.property.hostaway_listing_id,
            check_in=request.check_in,
            check_out=request.check_out,
            guests=request.guests,
            fallback_currency=request.property.currency_code,
        )
        duration_ms = max(0, round((self.timer() - started) * 1000))
        self.cache.set(
            cache_key,
            quote,
            timeout=settings.HOSTAWAY_PRICE_CACHE_TTL,
        )
        return quote, duration_ms, False

    @staticmethod
    def _validate_request(request: AvailabilityRequest) -> AvailabilityResult | None:
        nights = (
            (request.check_out - request.check_in).days
            if isinstance(request.check_in, date) and isinstance(request.check_out, date)
            else 0
        )
        if not request.property.is_visible or not request.property.hostaway_is_active:
            return AvailabilityService._result(PROPERTY_UNAVAILABLE, nights=max(0, nights))
        if not isinstance(request.check_in, date) or not isinstance(request.check_out, date):
            return AvailabilityService._result(INVALID_DATES, nights=0)
        if request.check_in < timezone.localdate():
            return AvailabilityService._result(PAST_CHECK_IN, nights=max(0, nights))
        if nights < 1 or nights > 366:
            return AvailabilityService._result(INVALID_DATES, nights=max(0, nights))
        if isinstance(request.guests, bool) or not isinstance(request.guests, int):
            return AvailabilityService._result(CAPACITY_EXCEEDED, nights=nights)
        if request.guests < 1:
            return AvailabilityService._result(CAPACITY_EXCEEDED, nights=nights)
        if (
            request.property.person_capacity is not None
            and request.guests > request.property.person_capacity
        ):
            return AvailabilityService._result(CAPACITY_EXCEEDED, nights=nights)
        return None

    @staticmethod
    def _result(
        reason_code: str,
        *,
        nights: int,
        calendar: CalendarFetch | None = None,
    ) -> AvailabilityResult:
        message_ar, message_en = MESSAGES[reason_code]
        return AvailabilityResult(
            is_available=reason_code == AVAILABLE,
            reason_code=reason_code,
            user_message_ar=message_ar,
            user_message_en=message_en,
            nights=nights,
            calendar_duration_ms=calendar.duration_ms if calendar else 0,
            calendar_cache_hit=calendar.cache_hit if calendar else False,
            calendar_document=calendar.document if calendar else None,
        )


def evaluate_calendar(
    days: tuple[CalendarDay, ...],
    *,
    check_in: date,
    check_out: date,
) -> str:
    """Apply conservative stay rules to an already validated calendar."""
    nights = (check_out - check_in).days
    if nights < 1 or nights > 366:
        return INVALID_DATES

    by_date = {day.date: day for day in days}
    stay_dates = tuple(check_in + timedelta(days=offset) for offset in range(nights))
    if any(day_date not in by_date for day_date in (*stay_dates, check_out)):
        return CALENDAR_INCOMPLETE

    arrival_day = by_date[check_in]
    departure_day = by_date[check_out]
    if arrival_day.closed_on_arrival is True:
        return CLOSED_ON_ARRIVAL
    if departure_day.closed_on_departure is True:
        return CLOSED_ON_DEPARTURE
    if arrival_day.minimum_stay is not None and nights < arrival_day.minimum_stay:
        return MINIMUM_STAY_NOT_MET
    if arrival_day.maximum_stay is not None and nights > arrival_day.maximum_stay:
        return MAXIMUM_STAY_EXCEEDED

    for day_date in stay_dates:
        day = by_date[day_date]
        if day.is_available is not True:
            return UNAVAILABLE_DATES
        if day.available_units is not None and day.available_units <= 0:
            return UNAVAILABLE_DATES
        if day.desired_units_to_sell is not None and day.desired_units_to_sell <= 0:
            return UNAVAILABLE_DATES
    return AVAILABLE


def component_title_ar(component: PriceComponent) -> str:
    """Return a safe Arabic label for a known component, with a neutral fallback."""
    names = {
        "baseRate": "سعر الإقامة",
        "cleaningFee": "رسوم التنظيف",
        "vat": "ضريبة القيمة المضافة",
        "salesTax": "الضريبة",
        "cityTax": "ضريبة المدينة",
        "damageDeposit": "تأمين الأضرار",
        "pricePerExtraPerson": "رسوم ضيف إضافي",
    }
    name = getattr(component, "name", "")
    return names.get(name) or getattr(component, "title", "") or "بند سعر"
