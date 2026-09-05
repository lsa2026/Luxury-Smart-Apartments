from datetime import timedelta
from decimal import Decimal
from unittest.mock import Mock

import pytest
from django.conf import settings
from django.contrib import admin
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.db import connection
from django.test import Client, RequestFactory, override_settings
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from apps.reservations.admin import BookingIntentAdmin, BookingQuoteAdmin
from apps.reservations.models import BookingIntent, BookingQuote
from apps.reservations.security import (
    SESSION_MARKER_KEY,
    hash_session_marker,
)
from apps.reservations.services.booking import (
    QuoteCreation,
    consume_revalidated_quote,
    create_quote_for_property,
)
from apps.reservations.signing import quote_reference
from apps.reservations.views import GuestDetailsView
from tests.test_booking_models_services import (
    guest_data,
    make_availability,
    make_property,
    make_quote,
)

pytestmark = pytest.mark.django_db


class RevalidationService:
    result: object
    calls = 0

    def __enter__(self) -> "RevalidationService":
        return self

    def __exit__(self, *args: object) -> None:
        pass

    def check(self, request: object, *, bypass_cache: bool) -> object:
        assert bypass_cache is True
        type(self).calls += 1
        return type(self).result

    def create_booking_quote(
        self,
        request: object,
        *,
        session_hash: str,
        selected_display_currency: str = "SAR",
        bypass_cache: bool = False,
    ) -> QuoteCreation:
        assert bypass_cache is True
        type(self).calls += 1
        quote = create_quote_for_property(
            type(self).result,
            property_obj=request.property,
            session_hash=session_hash,
            selected_display_currency=selected_display_currency,
        )
        return QuoteCreation(type(self).result, quote)


def owned_client_quote() -> tuple[Client, BookingQuote, str]:
    client = Client()
    session = client.session
    marker = "synthetic-session-marker"
    session[SESSION_MARKER_KEY] = marker
    session.save()
    quote = make_quote(make_property(), session_hash=hash_session_marker(marker))
    return client, quote, quote_reference(quote)


def form_data() -> dict[str, str]:
    return {
        "guest_first_name": "Test",
        "guest_last_name": "Guest",
        "guest_email": "test@example.invalid",
        "guest_phone": "0500000000",
        "billing_street1": "King Fahd Road 10",
        "billing_city": "Riyadh",
        "billing_state": "Riyadh",
        "billing_country": "SA",
        "billing_postcode": "12345",
        "special_requests": "Synthetic",
        "terms_accepted": "on",
        "privacy_accepted": "on",
        "idempotency_key": "i" * 32,
    }


def test_quote_page_is_rtl_session_owned_and_contains_no_internal_ids() -> None:
    client, quote, reference = owned_client_quote()
    response = client.get(f"/reservations/quotes/{reference}/")
    content = response.content.decode()
    assert response.status_code == 200
    assert 'dir="rtl"' in content
    assert "عرض سعر مؤقت" in content
    assert str(quote.hostaway_listing_id) not in content
    assert str(quote.pk) not in content
    assert quote.session_key_hash not in content
    assert 'name="total_price"' not in content
    assert 'name="guest_country_code"' not in content
    assert "+966 50 000 0000" in content
    assert 'inputmode="tel"' in content
    assert 'data-guest-journey' in content
    assert 'data-journey-panel="1"' in content
    assert 'data-journey-panel="2"' in content
    assert 'data-country-select' in content
    assert 'data-initial-step="1"' in content
    assert "عنوان الدفع" in content
    assert "عنوان الشارع" in content
    assert "المدينة" in content
    assert "المنطقة أو المحافظة" in content
    assert "الدولة" in content
    assert "الرمز البريدي" in content
    assert "الفوترة" not in content


@pytest.mark.parametrize(
    ("language", "labels"),
    [
        (
            "ar",
            (
                "عنوان الدفع",
                "عنوان الشارع",
                "المدينة",
                "المنطقة أو المحافظة",
                "الدولة",
                "الرمز البريدي",
                "متابعة إلى عنوان الدفع",
            ),
        ),
        (
            "en",
            (
                "Payment address",
                "Street address",
                "City",
                "State or region",
                "Country",
                "Postal code",
                "Continue to payment address",
            ),
        ),
        (
            "fr",
            (
                "Adresse de paiement",
                "Adresse",
                "Ville",
                "État ou région",
                "Pays",
                "Code postal",
                "Continuer vers l’adresse de paiement",
            ),
        ),
    ],
)
def test_payment_address_labels_follow_selected_language(
    language: str,
    labels: tuple[str, ...],
) -> None:
    client, _quote, reference = owned_client_quote()
    client.cookies[settings.LANGUAGE_COOKIE_NAME] = language

    response = client.get(f"/reservations/quotes/{reference}/")
    content = response.content.decode()

    assert response.status_code == 200
    for label in labels:
        assert label in content


def test_other_session_gets_generic_404_for_quote() -> None:
    _client, _quote, reference = owned_client_quote()
    assert Client().get(f"/reservations/quotes/{reference}/").status_code == 404


def test_opening_guest_form_never_calls_hostaway(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, _quote, reference = owned_client_quote()
    service = Mock()
    monkeypatch.setattr(GuestDetailsView, "service_class", service)
    response = client.get(f"/reservations/quotes/{reference}/")
    assert response.status_code == 200
    service.assert_not_called()


def test_guest_details_requires_csrf() -> None:
    client, _quote, reference = owned_client_quote()
    protected = Client(enforce_csrf_checks=True)
    protected.cookies = client.cookies
    protected_session = protected.session
    protected_session[SESSION_MARKER_KEY] = "synthetic-session-marker"
    protected_session.save()
    response = protected.post(
        f"/reservations/quotes/{reference}/guest-details/",
        form_data(),
    )
    assert response.status_code == 403


def test_guest_submit_revalidates_and_creates_local_intent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, quote, reference = owned_client_quote()
    RevalidationService.result = make_availability(quote.property)
    RevalidationService.calls = 0
    monkeypatch.setattr(GuestDetailsView, "service_class", RevalidationService)

    response = client.post(
        f"/reservations/quotes/{reference}/guest-details/",
        form_data(),
    )

    assert response.status_code == 302
    assert RevalidationService.calls == 1
    intent = BookingIntent.objects.get()
    assert intent.status == BookingIntent.Status.AWAITING_PAYMENT
    assert intent.guest_email == "test@example.invalid"
    assert intent.guest_phone == "+966500000000"
    assert intent.guest_country_code == "SA"
    assert "/reservations/requests/" in response.url


def test_expired_quote_is_revalidated_and_replaced_with_new_fx_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, quote, reference = owned_client_quote()
    BookingQuote.objects.filter(pk=quote.pk).update(
        expires_at=timezone.now() - timedelta(seconds=1)
    )
    RevalidationService.result = make_availability(quote.property)
    RevalidationService.calls = 0
    monkeypatch.setattr(GuestDetailsView, "service_class", RevalidationService)

    response = client.post(
        f"/reservations/quotes/{reference}/guest-details/",
        form_data(),
    )

    assert response.status_code == 302
    quote.refresh_from_db()
    replacement = BookingQuote.objects.exclude(pk=quote.pk).get()
    assert quote.status == BookingQuote.Status.EXPIRED
    assert replacement.status == BookingQuote.Status.ACTIVE
    assert replacement.payment_amount_sar == Decimal("500.25")
    assert replacement.exchange_rate_snapshot["payment_currency"] == "SAR"
    assert BookingIntent.objects.count() == 0


def test_invalid_guest_form_never_revalidates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, _quote, reference = owned_client_quote()
    RevalidationService.calls = 0
    monkeypatch.setattr(GuestDetailsView, "service_class", RevalidationService)
    data = form_data()
    data.pop("privacy_accepted")
    response = client.post(
        f"/reservations/quotes/{reference}/guest-details/",
        data,
    )
    assert response.status_code == 400
    assert RevalidationService.calls == 0
    assert 'data-initial-step="2"' in response.content.decode()


def test_intent_page_is_owned_masks_pii_and_says_not_confirmed() -> None:
    client, quote, _reference = owned_client_quote()
    intent = consume_revalidated_quote(
        quote_id=quote.pk,
        session_hash=quote.session_key_hash,
        idempotency_key="i" * 32,
        guest_data=guest_data(),
        revalidated=make_availability(quote.property),
    ).intent
    assert intent is not None
    response = client.get(f"/reservations/requests/{intent.public_reference}/")
    content = response.content.decode()
    assert response.status_code == 200
    assert 'dir="rtl"' in content
    assert "هذا الطلب غير مؤكد" in content
    assert "الدفع سيُفعّل" in content
    assert intent.guest_email not in content
    assert intent.guest_phone not in content
    assert str(quote.hostaway_listing_id) not in content
    assert Client().get(f"/reservations/requests/{intent.public_reference}/").status_code == 404


@override_settings(
    BOOKING_READ_RATE_LIMIT_REQUESTS=1,
    BOOKING_READ_RATE_LIMIT_WINDOW=300,
)
def test_quote_read_rate_limit() -> None:
    cache.clear()
    client, _quote, reference = owned_client_quote()
    assert client.get(f"/reservations/quotes/{reference}/").status_code == 200
    assert client.get(f"/reservations/quotes/{reference}/").status_code == 429


@override_settings(
    BOOKING_INTENT_RATE_LIMIT_REQUESTS=1,
    BOOKING_INTENT_RATE_LIMIT_WINDOW=300,
)
def test_guest_submission_rate_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cache.clear()
    client, quote, reference = owned_client_quote()
    RevalidationService.result = make_availability(quote.property)
    RevalidationService.calls = 0
    monkeypatch.setattr(GuestDetailsView, "service_class", RevalidationService)
    first = client.post(
        f"/reservations/quotes/{reference}/guest-details/",
        form_data(),
    )
    second = client.post(
        f"/reservations/quotes/{reference}/guest-details/",
        form_data(),
    )
    assert first.status_code == 302
    assert second.status_code == 429
    assert RevalidationService.calls == 1


@override_settings(
    BOOKING_READ_RATE_LIMIT_REQUESTS=1,
    BOOKING_READ_RATE_LIMIT_WINDOW=300,
)
def test_intent_read_rate_limit() -> None:
    cache.clear()
    client, quote, _reference = owned_client_quote()
    intent = consume_revalidated_quote(
        quote_id=quote.pk,
        session_hash=quote.session_key_hash,
        idempotency_key="i" * 32,
        guest_data=guest_data(),
        revalidated=make_availability(quote.property),
    ).intent
    assert intent is not None
    url = f"/reservations/requests/{intent.public_reference}/"
    assert client.get(url).status_code == 200
    assert client.get(url).status_code == 429


def test_quote_page_query_count_is_reasonable() -> None:
    client, _quote, reference = owned_client_quote()
    with CaptureQueriesContext(connection) as queries:
        response = client.get(f"/reservations/quotes/{reference}/")
    assert response.status_code == 200
    assert len(queries) <= 5


def test_admin_records_are_read_only_and_manual_add_is_blocked() -> None:
    user = get_user_model().objects.create_superuser(
        username="booking-admin",
        email="admin@example.invalid",
        password="synthetic-password",
    )
    request = RequestFactory().get("/admin/")
    request.user = user
    quote_admin = BookingQuoteAdmin(BookingQuote, admin.site)
    intent_admin = BookingIntentAdmin(BookingIntent, admin.site)
    assert quote_admin.has_add_permission(request) is False
    assert intent_admin.has_add_permission(request) is False
    assert intent_admin.has_delete_permission(request) is False
    assert set(quote_admin.fields) == set(quote_admin.readonly_fields)
    intent_fields = intent_admin.get_fields(request)
    assert set(intent_fields) == set(intent_admin.get_readonly_fields(request))
    assert "session_key_hash" not in intent_fields
