"""Synthetic-only owner invoice coverage; no live guest or payment operations."""

from concurrent.futures import ThreadPoolExecutor
from io import BytesIO
from unittest.mock import MagicMock, patch

import httpx
import pytest
from django.contrib.auth import get_user_model
from django.core import signing
from django.core.exceptions import ValidationError
from django.db import close_old_connections, connection
from django.test import Client, override_settings
from django.urls import reverse
from pypdf import PdfReader

from apps.integrations.hostaway.exceptions import HostawayResponseError
from apps.notifications.invoice_forms import InvoiceLookupForm, InvoiceReviewForm, InvoiceSendForm
from apps.notifications.invoice_pdf import render_invoice_pdf
from apps.notifications.models import GuestInvoice, GuestInvoiceDelivery
from apps.notifications.services.invoice_delivery import deliver_invoice
from apps.notifications.services.invoice_search import (
    SALT,
    find_invoice_bookings,
    read_search_token,
    search_token,
    stored_phone,
)
from apps.notifications.services.invoices import (
    invoice_snapshot,
    issue_invoice,
    prepare_invoice,
    queue_invoice_delivery,
    snapshot_token,
)
from apps.notifications.services.ultramsg import (
    UltraMsgClient,
    UltraMsgConnectionError,
    UltraMsgResponseError,
)
from apps.properties.models import Property
from apps.reservations.models import Reservation

pytestmark = pytest.mark.django_db


@pytest.mark.parametrize(
    "phone,country,expected",
    [
        ("0501234567", "SA", "+966501234567"),
        ("٥٠١٢٣٤٥٦٧", "SA", "+966501234567"),
        ("966501234567", "SA", "+966501234567"),
        ("00966501234567", "SA", "+966501234567"),
        ("+1 (202) 555-0100", "SA", "+12025550100"),
        ("06 12 34 56 78", "FR", "+33612345678"),
        ("06 12 34 56 78", "MA", "+212612345678"),
    ],
)
def test_invoice_lookup_normalizes_familiar_phone(phone, country, expected):
    form = InvoiceLookupForm({"guest_phone": phone, "guest_phone_country": country})
    assert form.is_valid(), form.errors
    assert form.cleaned_data["guest_phone"] == expected


@pytest.mark.parametrize(
    "data",
    [
        {},
        {"guest_phone": "123"},
        {"guest_phone": "0501234567", "guest_phone_country": "XX"},
        {"guest_phone": "abc+966501234567", "guest_phone_country": "SA"},
        {"hostaway_id": 88, "guest_phone": "0501234567"},
    ],
)
def test_invoice_lookup_rejects_ambiguous_or_invalid_input(data):
    assert not InvoiceLookupForm(data).is_valid()


def test_invoice_lookup_keeps_old_id_posts_valid():
    form = InvoiceLookupForm({"hostaway_id": 88001234})
    assert form.is_valid()
    assert InvoiceLookupForm().fields["guest_phone_country"].initial == "SA"


@pytest.mark.parametrize(
    "row,expected",
    [
        ({"phone": "00966501234567"}, "+966501234567"),
        ({"phone": "966501234567"}, "+966501234567"),
        ({"phone": "0501234567", "guestCountry": "SA"}, "+966501234567"),
        ({"phone": "0501234567"}, ""),
        ({"phone": ""}, ""),
        ({"phone": "1234", "guestCountry": "SA"}, ""),
    ],
)
def test_invoice_stored_phone_does_not_guess_unknown_local_country(row, expected):
    assert stored_phone(row) == expected


def test_phone_search_exact_match_and_no_persistence(payload):
    raw = {**payload["result"], "phone": "+966501234567"}
    provider = MagicMock()
    provider.invoice_search_page.return_value = [
        raw,
        {**raw, "id": 88001235, "phone": "+971501234567"},
    ]
    candidates, cursor = find_invoice_bookings(client=provider, phone="+966501234567")
    assert [item["id"] for item in candidates] == [88001234]
    assert cursor is None
    assert "must-not-persist" not in str(candidates)
    assert "guest_email" not in candidates[0] and "guest_phone" not in candidates[0]
    assert GuestInvoice.objects.count() == Reservation.objects.count() == 0


def test_phone_search_does_not_auto_choose_from_multiple_stays(client, actor, payload):
    client.force_login(actor)
    with patch("apps.notifications.invoice_views.HostawayClient") as factory:
        provider = factory.return_value.__enter__.return_value
        provider.invoice_search_page.return_value = [
            payload["result"],
            {**payload["result"], "id": 88001235},
        ]
        response = client.post(url("list"), {"guest_phone": "0016462817246"})
        provider.get_reservation_invoice_data.assert_not_called()
    assert response.status_code == 200
    assert len(response.context["candidates"]) == 2
    assert response.content.count("اختيار هذا الحجز".encode()) == 2
    assert GuestInvoice.objects.count() == Reservation.objects.count() == 0
    assert "no-store" in response["Cache-Control"]


def test_signed_phone_selection_rechecks_current_booking(client, actor, payload):
    client.force_login(actor)
    token = search_token(actor=actor, phone="+16462817246", reservation_id=88001234)
    with patch("apps.notifications.invoice_views.HostawayClient") as factory:
        provider = factory.return_value.__enter__.return_value
        provider.get_reservation_invoice_data.return_value = payload
        response = client.post(url("list"), {"phone_search_token": token})
        provider.get_reservation_invoice_data.assert_called_once_with(88001234)
        provider.invoice_search_page.assert_not_called()
    assert response.status_code == 302 and GuestInvoice.objects.count() == 1
    assert Reservation.objects.count() == 0


def test_phone_selection_changed_contact_does_not_create_invoice(client, actor, payload):
    client.force_login(actor)
    token = search_token(actor=actor, phone="+966501234567", reservation_id=88001234)
    with patch("apps.notifications.invoice_views.HostawayClient") as factory:
        factory.return_value.__enter__.return_value.get_reservation_invoice_data.return_value = (
            payload
        )
        response = client.post(url("list"), {"phone_search_token": token})
    assert response.status_code == 200
    assert "تغيّر رقم جوال الحجز" in response.content.decode()
    assert GuestInvoice.objects.count() == 0


@pytest.mark.parametrize("bad", ["bad-token", "", "tamper"])
def test_invoice_search_token_is_validated(actor, bad):
    with pytest.raises(ValidationError):
        read_search_token(bad, actor)


def test_phone_selection_wrong_actor_and_expired_token(actor):
    token = search_token(actor=actor, phone="+966501234567", reservation_id=88)
    other = get_user_model().objects.create_superuser(
        username="another-owner", email="other@example.invalid"
    )
    with pytest.raises(ValidationError):
        read_search_token(token, other)
    with patch("django.core.signing.time.time", return_value=0):
        old = search_token(actor=actor, phone="+966501234567", reservation_id=88)
    with pytest.raises(ValidationError):
        read_search_token(old, actor)
    malformed = signing.dumps(
        {"actor": str(actor.pk), "phone": "+966501234567", "id": True}, salt=SALT
    )
    with pytest.raises(ValidationError):
        read_search_token(malformed, actor)


def test_invalid_phone_selection_is_friendly_and_never_reads_provider(client, actor):
    client.force_login(actor)
    with patch("apps.notifications.invoice_views.HostawayClient") as factory:
        response = client.post(url("list"), {"phone_search_token": "tampered"})
        factory.assert_not_called()
    assert response.status_code == 200


def test_phone_search_bounded_and_has_explicit_continuation(payload, actor, client):
    rows = [{**payload["result"], "id": i, "phone": "+966501234567"} for i in range(1, 301)]
    provider = MagicMock()
    provider.invoice_search_page.side_effect = [rows[:100], rows[100:200]]
    candidates, cursor = find_invoice_bookings(client=provider, phone="+16462817246")
    assert candidates == [] and cursor == 200
    assert provider.invoice_search_page.call_count == 2
    client.force_login(actor)
    token = search_token(actor=actor, phone="+16462817246", cursor=200)
    with patch("apps.notifications.invoice_views.HostawayClient") as factory:
        provider = factory.return_value.__enter__.return_value
        provider.invoice_search_page.return_value = []
        response = client.post(url("list"), {"phone_search_token": token})
        provider.invoice_search_page.assert_called_once_with(after_id=200)
    assert response.status_code == 200 and response.context["continuation"] is None


@pytest.mark.parametrize(
    "change",
    [
        {"status": "cancelled"},
        {"listingMapId": 987654},
        {"arrivalDate": "2026-13-01"},
        {"departureDate": "2026-12-01"},
        {"phone": "+1646281724"},
    ],
)
def test_phone_search_skips_non_matching_or_invalid_stays(payload, change):
    provider = MagicMock()
    provider.invoice_search_page.return_value = [{**payload["result"], **change}]
    assert find_invoice_bookings(client=provider, phone="+16462817246") == ([], None)


def test_phone_page_uses_documented_cursor_without_phone_query(payload):
    from apps.integrations.hostaway.client import HostawayClient

    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, json={"status": "success", "result": [payload["result"]]})

    with httpx.Client(
        base_url="https://api.hostaway.com/v1", transport=httpx.MockTransport(handler)
    ) as http:
        with HostawayClient(access_token="test-token", client=http) as provider:
            assert provider.invoice_search_page(after_id=42)[0]["id"] == 88001234
    assert requests[0].url.params["afterId"] == "42"
    assert requests[0].url.params["sortOrder"] == "arrivalDateDesc"
    assert requests[0].url.params["limit"] == "100"
    assert "phone" not in str(requests[0].url) and "match" not in requests[0].url.params


def test_phone_lookup_nonowner_get_and_invalid_form_never_reads_hostaway(client, actor):
    with patch("apps.notifications.invoice_views.HostawayClient") as factory:
        assert client.post(url("list"), {"guest_phone": "0501234567"}).status_code == 302
        client.force_login(actor)
        response = client.get(url("list"))
        assert response.status_code == 200
        assert 'name="guest_phone"' in response.content.decode()
        assert client.post(url("list"), {"guest_phone": "123"}).status_code == 200
        factory.assert_not_called()


def test_selected_national_stored_phone_becomes_international_in_draft(actor, payload):
    payload["result"].update(phone="0501234567", guestCountry="SA")
    provider = MagicMock()
    provider.get_reservation_invoice_data.return_value = payload
    invoice = prepare_invoice(
        client=provider, hostaway_id=88001234, actor=actor, expected_phone="+966501234567"
    )
    assert invoice.snapshot["guest_phone"] == "+966501234567"


@pytest.mark.parametrize(
    "rows",
    [
        [{"id": True}],
        [{"id": 0}],
        [{"id": 1}, {"id": 1}],
        [{"id": i} for i in range(1, 102)],
    ],
)
def test_invoice_phone_api_rejects_unbounded_or_bad_identities(rows):
    from apps.integrations.hostaway.client import HostawayClient

    with HostawayClient(access_token="test-token") as provider:
        with patch.object(
            provider, "_get_json", return_value={"status": "success", "result": rows}
        ):
            with pytest.raises(HostawayResponseError):
                provider.invoice_search_page()


def test_phone_search_continuation_never_reports_complete_empty_result(client, actor, payload):
    client.force_login(actor)
    rows = [{**payload["result"], "id": i, "phone": "+966501234567"} for i in range(1, 201)]
    with patch("apps.notifications.invoice_views.HostawayClient") as factory:
        provider = factory.return_value.__enter__.return_value
        provider.invoice_search_page.side_effect = [rows[:100], rows[100:]]
        response = client.post(url("list"), {"guest_phone": "+16462817246"})
    assert response.status_code == 200 and response.context["continuation"]
    assert "لم يظهر حجز مطابق في هذه الدفعة" in response.content.decode()
    assert "متابعة البحث في الحجوزات الأقدم" in response.content.decode()


@pytest.fixture
def actor():
    return get_user_model().objects.create_superuser(
        username="invoice-owner", email="owner@example.invalid", password="test-only"
    )


@pytest.fixture
def property_obj():
    return Property.objects.create(
        hostaway_listing_id=8001,
        slug="invoice-unit",
        name_ar="شقة الملقا الذكية",
        name_en="Smart apartment in Al Malqa",
        name_fr="Appartement à Al Malqa",
        currency_code="SAR",
        is_visible=True,
    )


@pytest.fixture
def payload(property_obj):
    return {
        "status": "success",
        "result": {
            "id": 88001234,
            "listingMapId": property_obj.hostaway_listing_id,
            "channelId": 2002,
            "channelName": "Booking.com",
            "status": "new",
            "arrivalDate": "2026-12-15",
            "departureDate": "2026-12-17",
            "numberOfGuests": 2,
            "currency": "SAR",
            "totalPrice": "1400.00",
            "channelReservationId": "1234567890",
            "guestName": "ضيف تجريبي",
            "guestEmail": "guest@example.invalid",
            "phone": "+16462817246",
            "guestLocale": "ar",
            "doorCode": "must-not-persist",
            "creditCardNumber": "must-not-persist",
            "airbnbExpectedPayoutAmount": 1000,
        },
    }


@pytest.fixture
def draft(payload, actor):
    return GuestInvoice.objects.create(
        hostaway_reservation_id=88001234,
        created_by=actor,
        snapshot=invoice_snapshot(payload, 88001234),
    )


def reviewed(draft, language="ar"):
    return {
        **draft.snapshot,
        "language": language,
        "confirm_details": True,
        "snapshot_token": snapshot_token(draft),
    }


def test_stale_amount_cannot_be_issued_without_review(draft):
    old_review = reviewed(draft)
    draft.snapshot = {**draft.snapshot, "total": "1700.00"}
    draft.save()
    with pytest.raises(ValidationError):
        issue_invoice(draft.pk, old_review)
    draft.refresh_from_db()
    assert draft.issued_at is None
    assert issue_invoice(draft.pk, reviewed(draft)).snapshot["total"] == "1700.00"


def url(name, draft=None):
    return reverse(
        f"notifications:invoice_{name}", kwargs={"invoice_id": draft.pk} if draft else None
    )


def test_lookup_retains_only_required_fields(payload):
    snapshot = invoice_snapshot(payload, 88001234)
    assert snapshot["total"] == "1400.00"
    assert snapshot["platform_reference"] == "1234567890"
    assert snapshot["nights"] == 2
    assert "must-not-persist" not in str(snapshot)
    assert "payment_status" not in snapshot
    assert Reservation.objects.count() == 0


def test_airbnb_uses_guest_confirmation_not_internal_thread(payload):
    payload["result"].update(
        channelName="Airbnb",
        confirmationCode="HMP2KQ4B44",
        channelReservationId="10450-40160-thread313906227-9586017324",
    )
    assert invoice_snapshot(payload, 88001234)["platform_reference"] == "HMP2KQ4B44"


@pytest.mark.parametrize(
    "reference",
    ["88001234", "10450-40160-thread313906227-9586017324", "10450-40160-2000-9586017324", ""],
)
def test_internal_ids_never_substitute_missing_platform_reference(payload, reference):
    payload["result"]["channelReservationId"] = reference
    payload["result"]["reservationId"] = "internal-not-a-guest-reference"
    assert invoice_snapshot(payload, 88001234)["platform_reference"] == ""


@pytest.mark.parametrize("value", ["NaN", "Infinity", "0", "-10", "1.001", "100000000.00", None])
def test_invalid_or_ambiguous_amount_is_blocked(payload, value):
    payload["result"]["totalPrice"] = value
    with pytest.raises((ValidationError, HostawayResponseError)):
        invoice_snapshot(payload, 88001234)


@pytest.mark.parametrize("status", ["cancelled", "inquiry", "unconfirmed", "ownerStay"])
def test_non_stay_status_is_blocked(payload, status):
    payload["result"]["status"] = status
    with pytest.raises(ValidationError):
        invoice_snapshot(payload, 88001234)


def test_wrong_reservation_identity_is_blocked(payload):
    with pytest.raises(HostawayResponseError):
        invoice_snapshot(payload, 123)


def test_ambiguous_property_link_is_blocked(payload):
    Property.objects.create(
        hostaway_listing_id=9002, hostaway_listing_map_id=8001, slug="ambiguous", name_ar="شقة أخرى"
    )
    with pytest.raises(ValidationError):
        invoice_snapshot(payload, 88001234)


def test_external_currency_is_not_relabelled_but_direct_marrakech_is(payload, property_obj):
    property_obj.price_currency_override = "MAD"
    property_obj.save()
    assert invoice_snapshot(payload, 88001234)["currency"] == "SAR"
    payload["result"]["channelId"] = 2000
    assert invoice_snapshot(payload, 88001234)["currency"] == "MAD"


@pytest.mark.parametrize(
    "locale,expected", [("en-GB", "en"), ("fr_FR", "fr"), ("ar", "ar"), ("xx", "ar")]
)
def test_guest_language(payload, locale, expected):
    payload["result"]["guestLocale"] = locale
    assert invoice_snapshot(payload, 88001234)["language"] == expected


def test_prepare_is_read_only_and_preserves_issued_copy(payload, actor):
    client = MagicMock()
    client.get_reservation_invoice_data.return_value = payload
    draft = prepare_invoice(client=client, hostaway_id=88001234, actor=actor)
    invoice = issue_invoice(draft.pk, reviewed(draft))
    saved = bytes(invoice.pdf_content)
    assert prepare_invoice(client=client, hostaway_id=88001234, actor=actor).pdf_content == saved
    client.get_reservation_invoice_data.assert_called_once_with(88001234)
    assert len(client.mock_calls) == 1
    assert Reservation.objects.count() == 0


@pytest.mark.parametrize("language", ["ar", "en", "fr"])
def test_pdf_embedded_fonts_platform_reference_and_no_payment_or_hostaway_id(draft, language):
    invoice = issue_invoice(draft.pk, reviewed(draft, language))
    pdf = PdfReader(BytesIO(invoice.pdf_content))
    assert len(pdf.pages) == 1
    text = pdf.pages[0].extract_text()
    assert "1234567890" in text
    assert "88001234" not in text
    assert "1400" in text.replace(",", "")
    assert "Hostaway" not in text
    assert "Payment status" not in text
    assert "Statut du paiement" not in text
    assert len(bytes(invoice.pdf_content)) < 1_000_000
    assert any(
        "FontFile2" in str(font.get_object().get("/FontDescriptor", {}).get_object())
        for font in pdf.pages[0]["/Resources"]["/Font"].values()
        if font.get_object().get("/FontDescriptor")
    )


def test_double_issue_returns_exact_frozen_pdf_and_snapshot(draft):
    first = issue_invoice(draft.pk, reviewed(draft))
    second = issue_invoice(draft.pk, {**reviewed(draft), "guest_name": "changed"})
    assert first.number == second.number
    assert first.pdf_content == second.pdf_content
    assert second.snapshot["guest_name"] == "ضيف تجريبي"
    with pytest.raises(ValidationError):
        second.save()
    with pytest.raises(ValidationError):
        second.delete()


def test_preview_is_not_issued_and_number_not_assigned(draft):
    assert render_invoice_pdf(draft).startswith(b"%PDF-")
    draft.refresh_from_db()
    assert draft.number is None and draft.issued_at is None and not draft.pdf_content


def test_long_fields_have_single_page(draft):
    draft.snapshot["guest_name"] = "ضيف " * 40
    draft.snapshot["guest_email"] = "a" * 60 + "@" + "b" * 180 + ".invalid"
    draft.snapshot["platform_reference"] = "X" * 100
    draft.snapshot["property_names"]["ar"] = "شقة فاخرة في حي الملقا " * 10
    assert len(PdfReader(BytesIO(render_invoice_pdf(draft))).pages) == 1


def test_missing_platform_reference_requires_completion(draft):
    form = InvoiceReviewForm({**reviewed(draft), "platform_reference": ""})
    assert not form.is_valid() and "platform_reference" in form.errors


def test_contacts_optional_and_phone_normalized(draft):
    form = InvoiceReviewForm({**reviewed(draft), "guest_email": "", "guest_phone": ""})
    assert form.is_valid()
    form = InvoiceReviewForm({**reviewed(draft), "guest_phone": "0016462817246"})
    assert form.is_valid() and form.cleaned_data["guest_phone"] == "+16462817246"


@pytest.mark.parametrize("name", ["list", "detail", "pdf", "issue", "send"])
def test_non_owner_cannot_access_any_invoice_endpoint(draft, name):
    client = Client()
    assert client.get(url(name, None if name == "list" else draft)).status_code == 302
    user = get_user_model().objects.create_user(username="not-owner", is_staff=True)
    client.force_login(user)
    method = client.post if name in {"issue", "send"} else client.get
    assert method(url(name, None if name == "list" else draft)).status_code == 403


def test_no_mutation_via_get_and_csrf_is_required(draft, actor):
    client = Client(enforce_csrf_checks=True)
    client.force_login(actor)
    for name in ("issue", "send"):
        assert client.get(url(name, draft)).status_code == 405
        assert client.post(url(name, draft), reviewed(draft)).status_code == 403
    assert client.post(url("list"), {"hostaway_id": 88001234}).status_code == 403


def test_private_lookup_review_issue_download_and_missing_reference(client, actor, payload):
    client.force_login(actor)
    with patch("apps.notifications.invoice_views.HostawayClient") as provider:
        provider.return_value.__enter__.return_value.get_reservation_invoice_data.return_value = (
            payload
        )
        response = client.post(url("list"), {"hostaway_id": 88001234})
    assert response.status_code == 302
    draft = GuestInvoice.objects.get()
    assert client.get(url("detail", draft)).status_code == 200
    assert (
        client.post(
            url("issue", draft), {**reviewed(draft), "platform_reference": "88001234"}
        ).status_code
        == 200
    )
    draft.refresh_from_db()
    assert draft.issued_at is None
    assert client.post(url("issue", draft), reviewed(draft)).status_code == 302
    with patch("apps.notifications.invoice_views.HostawayClient") as provider:
        response = client.get(url("pdf", draft) + "?download=1")
        provider.assert_not_called()
    assert response.status_code == 200
    assert "private" in response["Cache-Control"] and "no-store" in response["Cache-Control"]
    assert response["Content-Type"] == "application/pdf"
    assert response["Content-Disposition"].startswith("attachment;")
    assert Reservation.objects.count() == 0


def test_lookup_error_is_safe_not_generic_500(client, actor):
    client.force_login(actor)
    with patch("apps.notifications.invoice_views.HostawayClient") as provider:
        provider.return_value.__enter__.return_value.get_reservation_invoice_data.side_effect = (
            HostawayResponseError("private provider response")
        )
        response = client.post(url("list"), {"hostaway_id": 88001234})
    assert response.status_code == 200
    assert b"private provider response" not in response.content


@override_settings(
    EMAIL_DELIVERY_ENABLED=True, EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend"
)
def test_email_is_explicit_once_and_contains_frozen_pdf(
    draft, actor, django_capture_on_commit_callbacks, mailoutbox
):
    invoice = issue_invoice(draft.pk, reviewed(draft, "fr"))
    with patch("apps.notifications.services.invoices.dispatch_invoice_delivery") as dispatch:
        with django_capture_on_commit_callbacks(execute=True):
            first = queue_invoice_delivery(
                invoice=invoice, channel="email", recipient="guest@example.invalid", actor=actor
            )
            second = queue_invoice_delivery(
                invoice=invoice, channel="email", recipient="guest@example.invalid", actor=actor
            )
        dispatch.assert_called_once_with(str(first.pk))
    assert first.pk == second.pk
    assert deliver_invoice(first.pk) == "accepted"
    assert deliver_invoice(first.pk) == "accepted"
    assert len(mailoutbox) == 1
    assert "Votre" in mailoutbox[0].subject
    assert mailoutbox[0].attachments[0].content == bytes(invoice.pdf_content)


@pytest.mark.parametrize("channel", ["email", "whatsapp"])
def test_disabled_delivery_is_blocked(draft, actor, channel):
    invoice = issue_invoice(draft.pk, reviewed(draft))
    with (
        override_settings(EMAIL_DELIVERY_ENABLED=False, ULTRAMSG_ENABLED=False),
        pytest.raises(ValidationError),
    ):
        queue_invoice_delivery(
            invoice=invoice, channel=channel, recipient="guest@example.invalid", actor=actor
        )
    assert GuestInvoiceDelivery.objects.count() == 0


def test_draft_cannot_be_sent(draft, actor):
    with pytest.raises(ValidationError):
        queue_invoice_delivery(
            invoice=draft, channel="email", recipient="guest@example.invalid", actor=actor
        )


@override_settings(EMAIL_DELIVERY_ENABLED=True)
@pytest.mark.parametrize(
    "status,dispatched",
    [("failed", True), ("review", False), ("accepted", False), ("sending", False)],
)
def test_owner_can_retry_only_explicitly_failed_delivery(
    draft, actor, django_capture_on_commit_callbacks, status, dispatched
):
    invoice = issue_invoice(draft.pk, reviewed(draft))
    delivery = GuestInvoiceDelivery.objects.create(
        invoice=invoice,
        channel="email",
        recipient="guest@example.invalid",
        requested_by=actor,
        status=status,
        attempt_count=1,
    )
    with patch("apps.notifications.services.invoices.dispatch_invoice_delivery") as dispatch:
        with django_capture_on_commit_callbacks(execute=True):
            outcome = queue_invoice_delivery(
                invoice=invoice, channel="email", recipient="guest@example.invalid", actor=actor
            )
        assert dispatch.called is dispatched
    assert outcome.pk == delivery.pk and outcome.attempt_count == 1


def test_wrong_owner_identity_is_denied_even_for_superuser(client, actor):
    client.force_login(actor)
    with override_settings(
        OPERATIONS_OWNER_ENFORCEMENT_ENABLED=True,
        OPERATIONS_OWNER_EMAIL="someoneelse@example.invalid",
    ):
        assert client.get(url("list")).status_code == 403


@pytest.mark.parametrize(
    "error,expected", [(UltraMsgConnectionError, "review"), (UltraMsgResponseError, "failed")]
)
def test_whatsapp_rejection_vs_uncertain_outcome_no_automatic_retry(draft, actor, error, expected):
    invoice = issue_invoice(draft.pk, reviewed(draft))
    delivery = GuestInvoiceDelivery.objects.create(
        invoice=invoice, channel="whatsapp", recipient="+16462817246", requested_by=actor
    )
    with patch("apps.notifications.services.invoice_delivery.UltraMsgClient") as provider:
        provider.return_value.__enter__.return_value.send_document.side_effect = error()
        assert deliver_invoice(delivery.pk) == expected
        assert deliver_invoice(delivery.pk) == expected
        provider.return_value.__enter__.return_value.send_document.assert_called_once()


@override_settings(EMAIL_DELIVERY_ENABLED=True)
def test_ambiguous_smtp_outcome_does_not_retry(draft, actor):
    invoice = issue_invoice(draft.pk, reviewed(draft))
    delivery = GuestInvoiceDelivery.objects.create(
        invoice=invoice, channel="email", recipient="guest@example.invalid", requested_by=actor
    )
    with patch(
        "apps.notifications.services.invoice_delivery.EmailMessage.send",
        side_effect=TimeoutError("secret"),
    ) as send:
        assert deliver_invoice(delivery.pk) == "review"
        assert deliver_invoice(delivery.pk) == "review"
        send.assert_called_once()
    delivery.refresh_from_db()
    assert delivery.error_code == "delivery_outcome_unknown"


@override_settings(
    ULTRAMSG_ENABLED=True, ULTRAMSG_INSTANCE_ID="test-instance", ULTRAMSG_TOKEN="test-only"
)
def test_whatsapp_uses_private_base64_and_safe_filename(draft):
    import base64
    from urllib.parse import parse_qs

    pdf = render_invoice_pdf(draft)
    calls = []

    def handle(request):
        calls.append(request)
        return httpx.Response(200, json={"sent": "true"})

    with httpx.Client(
        base_url="https://api.ultramsg.com", transport=httpx.MockTransport(handle)
    ) as http:
        UltraMsgClient(http=http).send_document(
            recipient="+16462817246", content=pdf, filename="invoice.pdf", caption="invoice"
        )
    data = parse_qs(calls[0].content.decode())
    assert calls[0].url.path.endswith("/messages/document")
    assert base64.b64decode(data["document"][0]) == pdf
    assert not data["document"][0].startswith("http")


@pytest.mark.parametrize(
    "channel,value",
    [
        ("email", "bad\r\nInjected:value"),
        ("whatsapp", "not a number"),
        ("email", "valid@example.invalid"),
    ],
)
def test_recipient_validation_and_explicit_consent(channel, value):
    form = InvoiceSendForm({"channel": channel, "recipient": value})
    assert not form.is_valid()


@pytest.mark.django_db(transaction=True)
def test_postgres_parallel_issuance_keeps_one_pdf(draft):
    if connection.vendor != "postgresql":
        pytest.skip("Row locking must be verified on PostgreSQL in CI, not SQLite.")

    def issue():
        close_old_connections()
        try:
            return bytes(issue_invoice(draft.pk, reviewed(draft)).pdf_content)
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: issue(), range(2)))
    assert results[0] == results[1]
