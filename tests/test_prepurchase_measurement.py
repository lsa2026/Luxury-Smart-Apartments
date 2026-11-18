"""Synthetic source/DOM contract tests; never send real provider or Google events."""

import json
from dataclasses import replace
from decimal import Decimal
from types import SimpleNamespace
from urllib.parse import quote as encode_cookie

import pytest
from django.conf import settings
from django.core.cache import cache
from django.template import Context
from django.test import Client, RequestFactory

from apps.core.marketing import EVENT_SCHEMAS, sanitize_analytics_event
from apps.payments.currency import CurrencyError, CurrencyService
from apps.payments.templatetags.currency import money_presentation
from apps.properties.models import Property
from apps.reservations import prepurchase
from apps.reservations.models import BookingQuote
from apps.reservations.views import AvailabilitySearchView
from tests.test_availability_views import DummyService, available_result, form_data, make_property
from tests.test_currency import fixed_rates

pytestmark = pytest.mark.django_db
ATTEMPT = "a" * 32
PARENT = "b" * 32


def consent(client, allowed=True):
    client.cookies["lsa_cookie_consent"] = encode_cookie(
        json.dumps(
            {
                "version": settings.COOKIE_CONSENT_VERSION,
                "analytics": allowed,
                "marketing": False,
            }
        )
    )


def request(allowed=True):
    r = RequestFactory().post(
        "/reservations/quotes/",
        {
            "measurement_attempt": ATTEMPT,
            "measurement_parent": PARENT,
            "measurement_entry": "availability_results",
            "measurement_journey": "home",
            "property": "private-key",
        },
    )
    r.session = {}
    r.COOKIES["lsa_cookie_consent"] = encode_cookie(
        json.dumps(
            {
                "version": settings.COOKIE_CONSENT_VERSION,
                "analytics": allowed,
                "marketing": False,
            }
        )
    )
    return r


@pytest.fixture
def service(monkeypatch):
    cache.clear()
    DummyService.calls = 0
    monkeypatch.setattr(AvailabilitySearchView, "service_class", DummyService)
    p = make_property()
    DummyService.result = available_result(p)
    return p


def test_safe_origin_parent_and_dates_not_exported():
    context = prepurchase.attempt_context(request())
    assert context["attempt_id"] == ATTEMPT
    assert context["parent_attempt_id"] == PARENT
    assert context["entry_page"] == "availability_results"
    assert context["journey_entry"] == "home"
    assert context["request_kind"] == "property_quote"
    assert "private-key" not in json.dumps(context)


def test_denied_consent_does_not_store_bridge_or_accept_client_identity():
    r = request(False)
    context = prepurchase.attempt_context(r)
    assert context["attempt_id"] != ATTEMPT
    assert "parent_attempt_id" not in context
    prepurchase.remember_quote(r, SimpleNamespace(pk="private-quote"), context)
    assert r.session == {}


def test_quote_bridge_is_bounded_and_old_quote_is_unlinked():
    r = request()
    for i in range(25):
        prepurchase.remember_quote(r, SimpleNamespace(pk=f"private-{i}"), {"attempt_id": ATTEMPT})
    assert len(r.session["pp1_quotes"]) == 20
    context = prepurchase.quote_context(
        r, SimpleNamespace(pk="unlinked-private-quote"), usable=True
    )
    assert context["prepurchase_result"] is None
    assert "attempt_id" not in context["prepurchase_context"]
    assert "unlinked-private-quote" not in json.dumps(context)


def test_public_page_kind_does_not_derive_private_url():
    r = request()
    r.resolver_match = SimpleNamespace(view_name="reservations:quote_detail")
    assert prepurchase.page_kind(r) == "quote"
    r.resolver_match = SimpleNamespace(view_name="unrecognized:private")
    assert prepurchase.page_kind(r) == "unknown"


def test_portfolio_success_and_empty_are_explicit_without_persisting_quote(service):
    c = Client()
    data = {**form_data(service), "property": ""}
    result = c.post("/reservations/quotes/", data)
    assert result.context["prepurchase_result"]["result_status"] == "available"
    assert result.context["prepurchase_result"]["result_count"] == 1
    assert 'data-pp-price-context="search_total"' in result.content.decode()
    assert BookingQuote.objects.count() == 0
    DummyService.result = replace(
        DummyService.result, is_available=False, reason_code="unavailable_dates", quote=None
    )
    result = c.post("/reservations/quotes/", data)
    assert result.context["prepurchase_result"]["result_status"] == "no_results"
    assert result.context["prepurchase_result"]["error_count"] == 0


def test_all_provider_failure_is_not_reported_or_shown_as_no_results(service):
    DummyService.result = replace(
        DummyService.result,
        is_available=False,
        reason_code="hostaway_temporarily_unavailable",
        quote=None,
    )
    result = Client().post("/reservations/quotes/", {**form_data(service), "property": ""})
    metadata = result.context["prepurchase_result"]
    assert metadata["result_status"] == "upstream_error"
    assert metadata["display_status"] == "verification_error"
    assert metadata["error_count"] == 1 and metadata["result_count"] == 0
    assert "No available properties were found" not in result.content.decode()
    assert "لم يتم العثور على" not in result.content.decode()


def test_partial_portfolio_error_is_visible_and_distinct(service, monkeypatch):
    Property.objects.create(
        hostaway_listing_id=7002,
        slug="synthetic-second",
        name_ar="شقة أخرى",
        city="Riyadh",
        currency_code="SAR",
        person_capacity=4,
        is_visible=True,
    )
    success = available_result(service)

    def mixed(self, availability_request, **kwargs):
        return (
            success
            if availability_request.property.pk == service.pk
            else replace(
                success,
                is_available=False,
                reason_code="pricing_unavailable",
                quote=None,
            )
        )

    monkeypatch.setattr(DummyService, "check", mixed)
    result = Client().post("/reservations/quotes/", {**form_data(service), "property": ""})
    metadata = result.context["prepurchase_result"]
    assert metadata["result_status"] == "partial_error"
    assert metadata["display_status"] == "partial_results"
    assert metadata["result_count"] == 1 and metadata["checked_count"] == 2
    assert metadata["error_count"] == 1
    assert "تعذر التحقق من بعض الشقق" in result.content.decode()


def test_specific_unavailable_vs_technical_error(service):
    for reason, expected in [
        ("unavailable_dates", "unavailable"),
        ("calendar_incomplete", "upstream_error"),
    ]:
        DummyService.result = replace(
            available_result(service), is_available=False, reason_code=reason, quote=None
        )
        result = Client().post("/reservations/quotes/", form_data(service))
        assert result.context["prepurchase_result"]["result_status"] == expected
        assert result.context["prepurchase_result"]["request_kind"] == "property_quote"


def test_invalid_and_rate_limited_never_call_provider(service, monkeypatch):
    result = Client().post("/reservations/quotes/", {})
    assert result.status_code == 400
    assert result.context["prepurchase_result"]["display_status"] == "invalid_request"
    assert DummyService.calls == 0
    monkeypatch.setattr("apps.reservations.views.is_rate_limited", lambda *a, **k: True)
    result = Client().post("/reservations/quotes/", form_data(service))
    assert result.status_code == 429
    assert result.context["prepurchase_result"]["result_status"] == "rate_limited"
    assert DummyService.calls == 0


def test_quote_success_redirect_preserves_attempt_and_no_dates_or_reference_in_metadata(service):
    c = Client()
    consent(c)
    data = {
        **form_data(service),
        "measurement_attempt": ATTEMPT,
        "measurement_parent": PARENT,
        "measurement_entry": "availability_results",
        "measurement_journey": "home",
    }
    result = c.post("/reservations/quotes/", data, follow=True)
    assert result.status_code == 200
    metadata = result.context["prepurchase_result"]
    assert metadata["attempt_id"] == ATTEMPT and metadata["parent_attempt_id"] == PARENT
    assert metadata["entry_page"] == "availability_results" and metadata["page_kind"] == "quote"
    assert metadata["journey_entry"] == "home" and metadata["display_status"] == "available"
    assert BookingQuote.objects.count() == 1
    saved = BookingQuote.objects.get()
    serialized = json.dumps(metadata)
    assert str(saved.pk) not in serialized and str(saved.check_in) not in serialized
    repeated = c.get(result.request["PATH_INFO"])
    assert repeated.context["prepurchase_result"]["attempt_id"] == ATTEMPT
    assert BookingQuote.objects.count() == 1


def test_money_data_is_exactly_the_presented_currency_and_explicit_fallback(monkeypatch):
    monkeypatch.setattr(CurrencyService, "get_rates", lambda self: fixed_rates())
    context = Context({"display_currency": "EUR"})
    value = money_presentation(context, Decimal("500"), "SAR")
    assert value["value"] == Decimal("100.00") and value["currency"] == "EUR"
    assert value["source_value"] == Decimal("500") and value["source_currency"] == "SAR"

    def unavailable(self):
        raise CurrencyError("unavailable")

    monkeypatch.setattr(CurrencyService, "get_rates", unavailable)
    fallback = money_presentation(Context({"display_currency": "EUR"}), Decimal("500"), "SAR")
    assert fallback["value"] == Decimal("500") and fallback["currency"] == "SAR"
    assert "money-conversion-unavailable" in fallback["text"]


@pytest.mark.parametrize(
    "bad",
    [
        {"attempt_id": "raw-booking-id"},
        {"value": float("nan")},
        {"value": float("inf")},
        {"result_status": "unrecognized"},
        {"guest_name": "private"},
        {"extra": {}},
        {"value": 0},
    ],
)
def test_new_contract_rejects_unsafe_values(bad):
    with pytest.raises(ValueError):
        sanitize_analytics_event(
            "price_view",
            {
                "measurement_version": "pp1",
                "price_context": "quote_total",
                "value": 100,
                "currency": "SAR",
                **bad,
            },
        )


def test_parameter_budget_and_independent_status_contract():
    for name in (
        "availability_view",
        "availability_request",
        "availability_result_view",
        "price_view",
    ):
        assert len(EVENT_SCHEMAS[name]) <= 25
    payload = sanitize_analytics_event(
        "availability_result_view",
        {
            "measurement_version": "pp1",
            "result_status": "upstream_error",
            "display_status": "verification_error",
            "error_count": 2,
        },
    )
    assert payload["result_status"] != payload["display_status"]
