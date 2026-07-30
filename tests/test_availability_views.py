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
from apps.reservations.services.availability import AvailabilityResult
from apps.reservations.views import AvailabilitySearchView

pytestmark = pytest.mark.django_db


def make_property() -> Property:
    return Property.objects.create(
        hostaway_listing_id=7001,
        slug="searchable-property",
        hostaway_name="Internal synthetic source name",
        hostaway_internal_name="Do not render this internal name",
        name_ar="شقة البحث",
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
        value=Decimal("620.00"),
        total=Decimal("620.00"),
        is_included_in_total=True,
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

    def __enter__(self) -> "DummyService":
        return self

    def __exit__(self, *args: object) -> None:
        pass

    def check(self, request: object) -> AvailabilityResult:
        type(self).calls += 1
        return type(self).result


@pytest.fixture
def mocked_service(monkeypatch: pytest.MonkeyPatch) -> type[DummyService]:
    DummyService.calls = 0
    monkeypatch.setattr(AvailabilitySearchView, "service_class", DummyService)
    return DummyService


def form_data(property_obj: Property) -> dict[str, str | int]:
    check_in = timezone.localdate() + timedelta(days=5)
    return {
        "property": property_obj.pk,
        "check_in": check_in.isoformat(),
        "check_out": (check_in + timedelta(days=2)).isoformat(),
        "guests": 2,
    }


def test_home_and_property_forms_are_rtl_and_do_not_call_hostaway() -> None:
    property_obj = make_property()
    response = Client().get("/")
    detail = Client().get(property_obj.get_absolute_url())
    assert response.status_code == 200
    assert detail.status_code == 200
    assert b'dir="rtl"' in response.content
    assert b"search-availability" in response.content
    assert b"search-availability" in detail.content


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
    assert first.status_code == 200
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
    assert response.status_code == 200
    assert len(queries) <= 7
