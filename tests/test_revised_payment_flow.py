"""Owner address/return flow and passive status reads; providers remain synthetic."""

import os
from pathlib import Path
from unittest.mock import patch

import pytest
from django.http import HttpResponse
from django.template.loader import render_to_string
from django.test import Client
from django.urls import reverse
from django.utils import translation

from apps.payments.models import PaymentAttempt
from apps.payments.payment_link_revisions import prepare_preview
from apps.payments.payment_link_views import COPY
from apps.payments.payment_links import complete_request_payment
from tests.test_contact_layout import _standalone
from tests.test_payment_link_revisions import activate, change, collect, owner_client
from tests.test_reservation_payment_links import (
    billing,
    form_data,
    isolated_settings,  # noqa: F401
    manual_stay,
    page_url,
)

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def revision_settings(settings):
    settings.HOSTAWAY_LIVE_MODIFICATION_ENABLED = True
    settings.HOSTAWAY_LIVE_EXTENSION_ENABLED = True


def review():
    client = owner_client()
    stay = manual_stay()
    preview = prepare_preview(stay.pk, change(stay).pk)
    url = reverse("notifications:updated_payment_link_review", args=[stay.pk, preview.pk])
    return client, stay, preview, url


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("billing_street1", ""),
        ("billing_country", "Saudi Arabia"),
        ("billing_state", "Riyadh Province"),
        ("billing_postcode", "12 345"),
    ],
)
def test_owner_address_is_validated_before_any_external_change_or_delivery(field, value):
    client, _, preview, url = review()
    data = billing() | {field: value, "confirm_updated_link": "on"}
    with patch("apps.payments.owner_payment_links.approve_preview") as approve:
        response = client.post(url, data)
    assert response.status_code == 200
    assert field in response.context["billing_form"].errors
    approve.assert_not_called()
    preview.refresh_from_db()
    assert "billing_address" not in preview.settlement_snapshot
    assert not preview.deliveries.exists()
    assert not preview.modification_request.hostaway_operations.exists()
    assert not PaymentAttempt.objects.exists()


def test_owner_address_survives_approval_and_guest_only_needs_explicit_consents():
    client, stay, preview, url = review()
    with patch(
        "apps.payments.owner_payment_links.approve_preview",
        side_effect=lambda pk: activate(preview)[0],
    ):
        response = client.post(url, billing() | {"confirm_updated_link": "on"})
        repeat = client.post(url, {"confirm_updated_link": "on"})
    assert response.url == repeat.url == reverse("notifications:booking_list")
    preview.refresh_from_db()
    assert preview.settlement_snapshot["billing_address"] == billing()
    assert preview.settlement_snapshot["billing_collected_by_admin"] is True
    assert preview.deliveries.count() == 1
    assert preview.modification_request.hostaway_operations.count() == 1
    assert preview.accepted_at is None
    guest = Client()
    page = guest.get(page_url(preview))
    assert page.context["saved_billing"] == billing()
    assert not page.context["form"].initial.get("terms_accepted")
    assert not page.context["form"].initial.get("house_rules_accepted")
    with (
        patch("apps.payments.payment_link_views.create_request_checkout") as checkout,
        patch(
            "apps.payments.payment_link_views._render_hyperpay_checkout",
            return_value=HttpResponse("synthetic checkout"),
        ),
    ):
        refused = guest.post(page_url(preview), {"payment_request_version": str(preview.pk)})
        assert refused.status_code == 200
        checkout.assert_not_called()
        data = {key: value for key, value in form_data().items() if not key.startswith("billing_")}
        data["payment_request_version"] = str(preview.pk)
        accepted = guest.post(page_url(preview), data)
    assert accepted.status_code == 200
    assert {key: checkout.call_args.kwargs["billing_data"][key] for key in billing()} == billing()
    assert checkout.call_args.kwargs["acceptance"]["consents"] == {
        "terms": True,
        "house_rules": True,
    }
    stay.refresh_from_db()
    assert stay.check_out == preview.modification_request.new_check_out


def test_queue_failure_keeps_review_and_never_falls_back_to_sync_send():
    client, _, preview, url = review()
    with (
        patch(
            "apps.payments.owner_payment_links.approve_preview",
            side_effect=lambda pk: activate(preview)[0],
        ),
        patch("apps.payments.owner_payment_links.queue_delivery", side_effect=RuntimeError),
    ):
        result = client.post(url, billing() | {"confirm_updated_link": "on"})
    assert result.url == url
    preview.refresh_from_db()
    assert preview.approved_at
    assert not preview.deliveries.exists()
    assert preview.modification_request.hostaway_operations.count() == 1


def test_status_read_detects_verified_payment_without_any_provider_or_scan():
    client, stay, preview, _ = review()
    active, _ = activate(preview)
    url = reverse("notifications:booking_list")
    params = {"payment_status_snapshot": "1", "ids": str(stay.pk)}
    with patch(
        "apps.reservations.services.guest_classification_history.ensure_refresh"
    ) as scan:
        before = client.get(url, params)
        payment = collect(stay, str(active.amount_sar), request=active)
        complete_request_payment(payment)
        after = client.get(url, params)
        again = client.get(url, params)
    scan.assert_not_called()
    assert before.status_code == after.status_code == 200
    assert before.json()["fingerprint"] != after.json()["fingerprint"]
    assert after.json() == again.json()
    assert set(after.json()) == {"fingerprint"}
    assert "no-store" in after["Cache-Control"]
    active.refresh_from_db()
    assert active.status == "paid"
    assert Client().get(url, params).status_code == 302
    assert client.get(url, params | {"ids": "invalid"}).status_code == 400
    assert client.get(url, params | {"ids": ",".join([str(stay.pk)] * 101)}).status_code == 400


def test_address_lookup_is_private_and_uses_the_standard_component(settings):
    settings.GOOGLE_PLACES_ENABLED = True
    settings.GOOGLE_PLACES_API_KEY = "synthetic-key"
    settings.GOOGLE_PLACES_REQUIRE_SHARED_CACHE = False
    client, _, preview, url = review()
    page = client.get(url, HTTP_ACCEPT_LANGUAGE="en")
    html = page.content.decode()
    assert page.context["address_places_enabled"]
    assert "data-checkout-address" in html
    assert 'name="billing_state"' in html and "data-morning-pattern" in html
    assert "عنوان الفوترة نيابةً عن الضيف" in html
    suggestions = reverse("notifications:updated_payment_address_suggestions", args=[preview.pk])
    assert Client().post(suggestions, {}).status_code == 302
    assert client.post(suggestions, data="{}", content_type="application/json").status_code == 400
    assert not preview.deliveries.exists()


@pytest.mark.parametrize("language", ["ar", "en", "fr"])
def test_render_revised_payment_layout(language, settings, tmp_path):
    settings.GOOGLE_PLACES_ENABLED = True
    settings.GOOGLE_PLACES_API_KEY = "synthetic-key"
    settings.GOOGLE_PLACES_REQUIRE_SHARED_CACHE = False
    client, _, preview, url = review()
    output = Path(os.environ.get("LSA_CONTACT_VISUAL_DIR", tmp_path))
    output.mkdir(parents=True, exist_ok=True)
    if language == "ar":
        page = client.get(url)
        assert page.status_code == 200
        html = _standalone(page.content.decode())
        (output / "revised-payment-owner.html").write_text(html)
    with translation.override(language):
        html = render_to_string(
            "payments/payment_link_receipt.html",
            {"ui": COPY[language], "language": language, "amount": 1500, "currency": "SAR"},
        )
    assert "luxury-living-lockup-light.png" in html
    assert "payment-outcome.css" in html
    assert "data-analytics-purchase-event" not in html
    (output / f"payment-receipt-{language}.html").write_text(_standalone(html))
