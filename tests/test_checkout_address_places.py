import json
import re
from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch

import httpx
import pytest
from django.core import signing
from django.core.cache import cache
from django.test import Client, override_settings
from django.utils import timezone, translation

from apps.payments.hyperpay.service import build_checkout_payload
from apps.reservations.address_places import (
    AUTOCOMPLETE_MASK,
    DETAILS_MASK,
    _key,
    _salt,
    address_fields,
    make_address_ticket,
)
from apps.reservations.models import BookingQuote
from apps.reservations.signing import quote_reference
from tests.test_billing_address_minimum import form
from tests.test_booking_models_services import make_quote
from tests.test_booking_views_admin import owned_client_quote

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def isolated_places():
    cache.clear()
    with override_settings(
        GOOGLE_PLACES_ENABLED=True,
        GOOGLE_PLACES_API_KEY="synthetic-key",
        GOOGLE_PLACES_REQUIRE_SHARED_CACHE=False,
        GOOGLE_PLACES_DAILY_LIMIT=300,
    ):
        yield
    cache.clear()


def component(kind, value, short=None):
    return {"types": [kind], "longText": value, "shortText": short or value}


def post(client, reference, ticket, *, details=False, **values):
    path = "address-details" if details else "address-suggestions"
    return client.post(
        f"/reservations/quotes/{reference}/{path}/",
        data=json.dumps({"ticket": ticket, **values}),
        content_type="application/json",
    )


@pytest.fixture
def google():
    with patch("apps.reservations.address_places.httpx.Client") as factory:
        provider = factory.return_value.__enter__.return_value
        provider.post.return_value.json.return_value = {
            "suggestions": [
                {
                    "placePrediction": {
                        "placeId": "ChIJ-test_123",
                        "text": {"text": "10 King Street, London, UK"},
                    }
                }
            ]
        }
        provider.get.return_value.json.return_value = {
            "addressComponents": [
                component("street_number", "10"),
                component("route", "King Street"),
                component("postal_town", "London"),
                component("administrative_area_level_1", "England"),
                component("country", "United Kingdom", "GB"),
                component("postal_code", "SW1A 1AA"),
            ],
            "formattedAddress": "10 King Street, London SW1A 1AA, UK",
        }
        yield provider


def test_quote_owned_csrf_protected_essentials_only_session_and_rotation(google):
    client, quote, reference = owned_client_quote()
    ticket = make_address_ticket(quote)
    suggestions = post(client, reference, ticket, input="10 King Street")
    assert suggestions.status_code == 200
    assert "no-store" in suggestions["Cache-Control"]
    assert suggestions["X-Robots-Tag"] == "noindex, nofollow"
    call = google.post.call_args
    assert call.args == ("https://places.googleapis.com/v1/places:autocomplete",)
    assert call.kwargs["headers"]["X-Goog-FieldMask"] == AUTOCOMPLETE_MASK
    payload = call.kwargs["json"]
    assert "includedRegionCodes" not in payload
    assert "locationRestriction" not in payload
    assert "sessionToken" in payload
    details = post(client, reference, ticket, details=True, place_id="ChIJ-test_123")
    assert details.status_code == 200
    assert details.json()["fields"]["billing_postcode"] == "SW1A 1AA"
    assert details.json()["fields"]["billing_country"] == "GB"
    assert google.get.call_args.kwargs["headers"]["X-Goog-FieldMask"] == DETAILS_MASK
    assert google.get.call_args.kwargs["params"]["sessionToken"] == payload["sessionToken"]
    renewed = details.json()["next_ticket"]
    assert signing.loads(renewed, salt=_salt(quote))["nonce"] != payload["sessionToken"]
    reused = post(client, reference, ticket, details=True, place_id="ChIJ-test_123")
    assert reused.status_code == 409
    assert google.get.call_count == 1
    # Cache metadata contains neither typed text nor Google suggestion labels.
    cached = str(getattr(cache, "_cache", {}))
    assert "King Street" not in cached
    assert "synthetic-key" not in cached
    assert "SW1A" not in cached


def test_details_cannot_proxy_arbitrary_places_or_fields(google):
    client, quote, reference = owned_client_quote()
    ticket = make_address_ticket(quote)
    assert (
        post(client, reference, ticket, details=True, place_id="ChIJ-arbitrary").status_code == 400
    )
    assert post(client, reference, ticket, input="London", fields="*").status_code == 400
    assert post(client, reference, ticket, details=True, place_id="../evil").status_code == 400
    assert post(client, reference, ticket, input="Lo\nndon").status_code == 400
    assert post(client, reference, ticket, input="x" * 151).status_code == 400
    assert post(client, reference, "forged", input="London").status_code == 400
    google.post.assert_not_called()
    google.get.assert_not_called()


def test_other_browser_and_quote_cannot_use_ticket(google):
    client, quote, reference = owned_client_quote()
    ticket = make_address_ticket(quote)
    assert post(Client(), reference, ticket, input="London").status_code == 404
    other_quote = make_quote(quote.property, session_hash=quote.session_key_hash)
    assert post(client, quote_reference(other_quote), ticket, input="London").status_code == 400
    quote.status = BookingQuote.Status.CONSUMED
    quote.save(update_fields=["status"])
    assert post(client, reference, ticket, input="London").status_code == 409
    google.post.assert_not_called()


def test_expired_quote_and_expired_ticket_never_call_google(google):
    client, quote, reference = owned_client_quote()
    ticket = make_address_ticket(quote)
    with patch("django.core.signing.time.time", return_value=timezone.now().timestamp() + 601):
        response = post(client, reference, ticket, input="London")
    assert response.status_code == 409
    assert response.json()["detail"] == "session_expired"
    assert response.json()["next_ticket"] != ticket
    quote.expires_at = timezone.now() - timedelta(seconds=1)
    quote.save(update_fields=["expires_at"])
    assert post(client, reference, ticket, input="London").status_code == 409
    google.post.assert_not_called()


def test_csrf_is_not_exempt_and_get_is_not_supported(google):
    client, quote, reference = owned_client_quote()
    path = f"/reservations/quotes/{reference}/address-suggestions/"
    assert client.get(path).status_code == 405
    protected = Client(enforce_csrf_checks=True)
    protected.cookies = client.cookies
    assert post(protected, reference, make_address_ticket(quote), input="London").status_code == 403
    google.post.assert_not_called()


def test_disabled_or_cache_unavailable_falls_back_without_paid_call(google):
    client, quote, reference = owned_client_quote()
    ticket = make_address_ticket(quote)
    with override_settings(GOOGLE_PLACES_ENABLED=False):
        assert post(client, reference, ticket, input="London").status_code == 503
        page = client.get(f"/reservations/quotes/{reference}/").content.decode()
        assert "checkout-address.js" not in page
        assert 'name="billing_street1"' in page
    with override_settings(GOOGLE_PLACES_REQUIRE_SHARED_CACHE=True):
        assert post(client, reference, ticket, input="London").status_code == 503
    with patch("apps.reservations.address_places.cache.get", side_effect=RuntimeError("offline")):
        assert post(client, reference, ticket, input="London").status_code == 503
    google.post.assert_not_called()


def test_daily_cap_combines_autocomplete_details_and_short_circuits_client(google):
    client, quote, reference = owned_client_quote()
    ticket = make_address_ticket(quote)
    with override_settings(GOOGLE_PLACES_DAILY_LIMIT=1):
        assert post(client, reference, ticket, input="London").status_code == 200
        assert (
            post(client, reference, ticket, details=True, place_id="ChIJ-test_123").status_code
            == 429
        )
    google.get.assert_not_called()
    cache.clear()
    cache.set(_key("client", quote.session_key_hash), 25, timeout=600)
    assert post(client, reference, ticket, input="London").status_code == 429
    assert cache.get(f"places:day:{timezone.now().date().isoformat()}") is None
    assert google.post.call_count == 1


def test_provider_failure_and_bad_json_never_log_address_or_key(google, caplog):
    client, quote, reference = owned_client_quote()
    ticket = make_address_ticket(quote)
    google.post.side_effect = httpx.ReadTimeout("secret synthetic-key private London")
    assert post(client, reference, ticket, input="private London").status_code == 503
    assert "private London" not in caplog.text
    assert "synthetic-key" not in caplog.text
    google.post.side_effect = None
    google.post.return_value.json.return_value = None
    assert post(client, reference, ticket, input="London").status_code == 503


@pytest.mark.parametrize(
    ("country", "postal"),
    [
        ("SA", "12345"),
        ("MA", "40000"),
        ("FR", "75001"),
        ("GB", "SW1A 1AA"),
        ("CA", "M5V 3L9"),
        ("US", "10001-1234"),
        ("JP", "100-0001"),
    ],
)
def test_international_components_preserve_postal_format_and_payment_payload(country, postal):
    values = address_fields(
        {
            "addressComponents": [
                component("country", "Country", country),
                component("postal_code", postal),
                component("locality", "City"),
                component("administrative_area_level_1", "Region"),
                component("street_number", "12"),
                component("route", "Street"),
            ]
        }
    )
    submitted = form(**values)
    assert submitted.is_valid(), submitted.errors
    assert submitted.cleaned_data["billing_postcode"] == postal
    assert values["billing_country"] == country
    intent = SimpleNamespace(
        **values,
        guest_email="guest@example.invalid",
        guest_first_name="Guest",
        guest_last_name="Example",
    )
    payload = build_checkout_payload(
        intent, merchant_id="synthetic", amount=Decimal("50.00"), currency="SAR"
    )
    assert payload["billing.country"] == country
    assert payload["billing.postcode"] == re.sub(r"[ -]", "", postal)
    assert re.fullmatch(r"[A-Za-z0-9]{1,16}", payload["billing.postcode"])
    assert values["billing_postcode"] == postal


@pytest.mark.parametrize(
    ("label", "short", "expected"),
    [
        ("New York", "NY", "NY"),
        ("Ontario", "ON", "ON"),
        ("Île-de-France", "IDF", "IDF"),
        ("Riyadh Province", "Riyadh Province", "RiyadhProvince"),
        ("Marrakesh-Safi", "Marrakesh-Safi", "MarrakeshSafi"),
    ],
)
def test_region_abbreviations_and_gateway_only_formatting(label, short, expected):
    values = address_fields(
        {
            "addressComponents": [
                component("administrative_area_level_1", label, short),
            ]
        }
    )
    assert values["billing_state"] == short
    submitted = form(**values)
    assert submitted.is_valid()
    intent = SimpleNamespace(**submitted.cleaned_data)
    payload = build_checkout_payload(intent, "synthetic", amount=Decimal("50.00"), currency="SAR")
    assert payload["billing.state"] == expected
    assert re.fullmatch(r"[a-zA-Z0-9.]{1,50}", payload["billing.state"])
    assert submitted.cleaned_data["billing_state"] == short


def test_missing_or_unrepresentable_region_does_not_invent_a_payment_value():
    assert "billing_state" not in address_fields(
        {
            "addressComponents": [
                component("administrative_area_level_1", "منطقة الرياض"),
            ]
        }
    )
    # Existing manual Unicode input is not mangled by deleting its letters.
    original = "منطقة الرياض"
    submitted = form(billing_state=original)
    assert submitted.is_valid()
    intent = SimpleNamespace(**submitted.cleaned_data)
    payload = build_checkout_payload(intent, "synthetic", amount=Decimal("50.00"), currency="SAR")
    assert payload["billing.state"] == original


def test_manual_latin_region_keeps_original_spelling_in_record():
    submitted = form(billing_state="Île-de-France")
    assert submitted.is_valid()
    intent = SimpleNamespace(**submitted.cleaned_data)
    payload = build_checkout_payload(intent, "synthetic", amount=Decimal("50.00"), currency="SAR")
    assert payload["billing.state"] == "IledeFrance"
    assert intent.billing_state == "Île-de-France"


def test_missing_invalid_and_overlong_components_are_not_invented_or_truncated():
    assert address_fields({"addressComponents": None}) == {}
    assert address_fields({"addressComponents": []}) == {}
    assert (
        address_fields(
            {
                "addressComponents": [
                    component("country", "Unknown", "XX"),
                    component("route", "x" * 101),
                    component("postal_code", "12 34!"),
                    component("administrative_area_level_1", "x" * 51),
                ]
            }
        )
        == {}
    )
    assert (
        address_fields(
            {
                "addressComponents": [
                    component("country", "United States", "US"),
                    component("postal_code", "10001"),
                    component("postal_code_suffix", "1234"),
                ]
            }
        )["billing_postcode"]
        == "10001-1234"
    )


@pytest.mark.parametrize(
    ("language", "label"),
    [
        ("ar", "ابحث عن عنوان الدفع (اختياري)"),
        ("en", "Find your payment address (optional)"),
        ("fr", "Rechercher votre adresse de paiement (facultatif)"),
    ],
)
def test_shared_quote_page_translated_private_search_and_manual_fields(language, label, google):
    client, quote, reference = owned_client_quote()
    client.cookies["django_language"] = language
    response = client.get(f"/reservations/quotes/{reference}/")
    content = response.content.decode()
    assert label in content
    assert "data-checkout-address hidden" in content
    assert 'name="payment-address-search"' not in content
    assert quote.session_key_hash not in content
    assert "synthetic-key" not in content
    assert 'translate="no" lang="en" dir="ltr">Google Maps' in content
    for name in ("street1", "city", "state", "country", "postcode"):
        assert f'name="billing_{name}"' in content
    google.post.assert_not_called()
    with translation.override(language):
        token = make_address_ticket(quote)
    assert signing.loads(token, salt=_salt(quote))["language"] == language


@pytest.mark.parametrize("language", ["ar", "en", "fr"])
@pytest.mark.parametrize("country", ["FR", "ZZ", ""])
def test_country_draft_marker_distinguishes_fresh_page_from_submitted_form(language, country):
    client, _quote, reference = owned_client_quote()
    client.cookies["django_language"] = language
    fresh = client.get(f"/reservations/quotes/{reference}/")
    assert fresh.status_code == 200
    assert 'data-draft-server-bound="false"' in fresh.content.decode()

    # Missing required guest fields keeps this a validation response: no
    # booking or payment is created. Even an invalid/empty submitted country
    # must be corrected by the guest rather than replaced by an old draft.
    submitted = client.post(
        f"/reservations/quotes/{reference}/guest-details/",
        {"billing_country": country},
    )
    assert submitted.status_code == 400
    assert 'data-draft-server-bound="true"' in submitted.content.decode()
    assert submitted.context["guest_form"]["billing_country"].value() == country
