from datetime import timedelta
from decimal import Decimal

import pytest
from django.core.cache import cache
from django.db import connection
from django.test import Client, override_settings
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from apps.integrations.hostaway.availability_validators import (
    PriceComponent,
    PriceQuote,
)
from apps.properties.models import Property
from apps.reservations.models import BookingQuote
from apps.reservations.services.availability import AvailabilityResult
from apps.reservations.services.booking import (
    QuoteCreation,
    create_quote_for_property,
)
from apps.reservations.views import AvailabilitySearchView

pytestmark = pytest.mark.django_db


def make_property() -> Property:
    return Property.objects.create(
        hostaway_listing_id=7001,
        slug="searchable-property",
        hostaway_name="Internal synthetic source name",
        hostaway_internal_name="Do not render this internal name",
        name_ar="شقة البحث",
        city="Riyadh",
        city_ar="الرياض",
        currency_code="SAR",
        person_capacity=4,
        is_visible=True,
    )


def available_result(property_obj: Property) -> AvailabilityResult:
    check_in = timezone.localdate() + timedelta(days=5)
    check_out = check_in + timedelta(days=2)
    component = PriceComponent(
        listing_fee_setting_id=None,
        type="price",
        name="baseRate",
        title="Base rate",
        alias="technical-alias",
        quantity=None,
        value=Decimal("310.00"),
        total=Decimal("620.00"),
        is_included_in_total=True,
        is_deleted=False,
    )
    quote = PriceQuote(
        listing_id=property_obj.hostaway_listing_id,
        check_in=check_in,
        check_out=check_out,
        nights=2,
        guests=2,
        currency="SAR",
        total_price=Decimal("620.00"),
        components=(component,),
        calculated_at=timezone.now(),
        envelope_fields=frozenset(),
        result_field_types=(),
        component_field_types=(),
    )
    return AvailabilityResult(
        is_available=True,
        reason_code="available",
        user_message_ar="الفترة متاحة.",
        user_message_en="Available.",
        nights=2,
        quote=quote,
    )


class DummyService:
    result: AvailabilityResult
    calls = 0
    last_bypass_cache: bool | None = None

    def __enter__(self) -> "DummyService":
        return self

    def __exit__(self, *args: object) -> None:
        pass

    def create_booking_quote(
        self,
        request: object,
        *,
        session_hash: str,
        bypass_cache: bool = False,
    ) -> QuoteCreation:
        type(self).calls += 1
        type(self).last_bypass_cache = bypass_cache
        quote = create_quote_for_property(
            type(self).result,
            property_obj=request.property,
            session_hash=session_hash,
        )
        return QuoteCreation(type(self).result, quote)

    def check(
        self,
        request: object,
        *,
        bypass_cache: bool = False,
    ) -> AvailabilityResult:
        type(self).calls += 1
        type(self).last_bypass_cache = bypass_cache
        return type(self).result


@pytest.fixture
def mocked_service(monkeypatch: pytest.MonkeyPatch) -> type[DummyService]:
    DummyService.calls = 0
    DummyService.last_bypass_cache = None
    monkeypatch.setattr(AvailabilitySearchView, "service_class", DummyService)
    return DummyService


def form_data(property_obj: Property) -> dict[str, str | int]:
    check_in = timezone.localdate() + timedelta(days=5)
    return {
        "city": "Riyadh",
        "property": property_obj.pk,
        "check_in": check_in.isoformat(),
        "check_out": (check_in + timedelta(days=2)).isoformat(),
        "guests": 2,
    }


def test_city_only_search_lists_available_properties_without_creating_quote(
    mocked_service: type[DummyService],
) -> None:
    property_obj = make_property()
    DummyService.result = available_result(property_obj)
    data = form_data(property_obj)
    data["property"] = ""

    response = Client().post("/reservations/quotes/", data)
    content = response.content.decode()

    assert response.status_code == 200
    assert "الوحدات المتاحة في الرياض" in content
    assert property_obj.name_ar in content
    assert "اختيار هذه الوحدة" in content
    assert f'{property_obj.get_absolute_url()}?source=availability&amp;' in content
    assert "check_in=" in content
    assert "check_out=" in content
    assert "guests=2" in content
    assert BookingQuote.objects.count() == 0
    assert mocked_service.calls == 1
    assert mocked_service.last_bypass_cache is True


def test_available_property_detail_preserves_search_without_repeating_fields() -> None:
    property_obj = make_property()
    check_in = timezone.localdate() + timedelta(days=5)
    check_out = check_in + timedelta(days=2)
    response = Client().get(
        property_obj.get_absolute_url(),
        {
            "source": "availability",
            "check_in": check_in.isoformat(),
            "check_out": check_out.isoformat(),
            "guests": 2,
        },
    )
    content = response.content.decode()

    assert response.status_code == 200
    assert "متابعة الحجز" in content
    assert "لا تحتاج إلى البحث مرة أخرى" in content
    assert f'name="check_in" value="{check_in.isoformat()}"' in content
    assert f'name="check_out" value="{check_out.isoformat()}"' in content
    assert 'name="guests" value="2"' in content
    assert "data-availability-form" not in content


def test_invalid_available_search_context_falls_back_to_normal_availability_form() -> None:
    property_obj = make_property()
    response = Client().get(
        property_obj.get_absolute_url(),
        {
            "source": "availability",
            "check_in": "not-a-date",
            "check_out": "not-a-date",
            "guests": 2,
        },
    )
    content = response.content.decode()

    assert response.status_code == 200
    assert "data-availability-form" in content
    assert "متابعة الحجز" not in content


def test_home_and_property_forms_are_rtl_and_do_not_call_hostaway() -> None:
    property_obj = make_property()
    response = Client().get("/")
    detail = Client().get(property_obj.get_absolute_url())
    assert response.status_code == 200
    assert detail.status_code == 200
    assert b'dir="rtl"' in response.content
    assert b"/reservations/quotes/" in response.content
    assert b"/reservations/quotes/" in detail.content


def test_availability_endpoint_rejects_get() -> None:
    assert Client().get("/properties/search-availability/").status_code == 405


def test_availability_post_requires_csrf() -> None:
    property_obj = make_property()
    client = Client(enforce_csrf_checks=True)
    response = client.post(
        "/properties/search-availability/",
        form_data(property_obj),
    )
    assert response.status_code == 403


def test_price_result_is_rtl_and_hides_internal_fields(
    mocked_service: type[DummyService],
) -> None:
    property_obj = make_property()
    DummyService.result = available_result(property_obj)
    client = Client(enforce_csrf_checks=True)
    home = client.get("/")
    token = home.cookies["csrftoken"].value
    response = client.post(
        "/properties/search-availability/",
        form_data(property_obj),
        HTTP_X_CSRFTOKEN=token,
        follow=True,
    )
    content = response.content.decode()
    assert response.status_code == 200
    assert 'dir="rtl"' in content
    assert "620" in content
    assert "SAR" in content
    assert "سعر الإقامة" in content
    assert "technical-alias" not in content
    assert "Do not render this internal name" not in content
    assert "إنشاء حجز" not in content
    assert mocked_service.calls == 1
    assert mocked_service.last_bypass_cache is True
    assert "310.00" not in content


def test_quote_ignores_browser_price_and_hostaway_listing_id(
    mocked_service: type[DummyService],
) -> None:
    property_obj = make_property()
    DummyService.result = available_result(property_obj)
    data = form_data(property_obj)
    data["total_price"] = "0.01"
    data["hostaway_listing_id"] = 999999
    response = Client().post("/reservations/quotes/", data)
    quote = BookingQuote.objects.get()
    assert response.status_code == 302
    assert quote.total_price == Decimal("620.0000")
    assert quote.hostaway_listing_id == property_obj.hostaway_listing_id
    assert mocked_service.calls == 1


@override_settings(
    AVAILABILITY_RATE_LIMIT_REQUESTS=1,
    AVAILABILITY_RATE_LIMIT_WINDOW=300,
)
def test_local_rate_limit_uses_session_cache(
    mocked_service: type[DummyService],
) -> None:
    cache.clear()
    property_obj = make_property()
    DummyService.result = available_result(property_obj)
    client = Client()
    first = client.post("/properties/search-availability/", form_data(property_obj))
    second = client.post("/properties/search-availability/", form_data(property_obj))
    assert first.status_code == 302
    assert second.status_code == 429
    assert mocked_service.calls == 1


def test_invalid_form_never_calls_service(
    mocked_service: type[DummyService],
) -> None:
    property_obj = make_property()
    data = form_data(property_obj)
    data["check_out"] = data["check_in"]
    response = Client().post("/properties/search-availability/", data)
    assert response.status_code == 400
    assert mocked_service.calls == 0


def test_search_query_count_is_bounded(
    mocked_service: type[DummyService],
) -> None:
    property_obj = make_property()
    DummyService.result = available_result(property_obj)
    client = Client()
    with CaptureQueriesContext(connection) as queries:
        response = client.post(
            "/properties/search-availability/",
            form_data(property_obj),
        )
    assert response.status_code == 302
    assert len(queries) <= 12
