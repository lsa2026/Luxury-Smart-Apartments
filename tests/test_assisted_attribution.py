"""Isolated synthetic joins; real HTTP/provider/Google transmission is forbidden."""

import json
from datetime import timedelta
from types import SimpleNamespace
from urllib.parse import parse_qs, quote, urlsplit

import pytest
from django.conf import settings
from django.contrib.auth.models import AnonymousUser
from django.core.exceptions import ValidationError
from django.test import Client, RequestFactory
from django.urls import reverse
from django.utils import timezone

from apps.core import assisted_attribution as aa
from apps.core.attribution_models import AttributionHandoff, BookingAttribution
from apps.core.marketing import EVENT_SCHEMAS, prepare_purchase_event, sanitize_analytics_event
from apps.core.models import MarketingEventReceipt, SiteSetting
from apps.payments.purchase_receipts import receipt_for, redirect_verified_purchase
from tests.test_day_use08 import rig as day_use_rig  # noqa: F401
from tests.test_payment_link_purchase_receipts import callback, google_settings, paid  # noqa: F401
from tests.test_reservation_payment_links import isolated_settings  # noqa: F401

pytestmark = pytest.mark.django_db
CAMPAIGN = "24262302633"
OTHER_CAMPAIGN = "24262298844"


def visitor(*, allowed=True, staff=False):
    r = RequestFactory().get("/ar/")
    r.session = {}
    r.user = SimpleNamespace(is_staff=True) if staff else AnonymousUser()
    r.COOKIES["lsa_cookie_consent"] = quote(
        json.dumps(
            {
                "version": settings.COOKIE_CONSENT_VERSION,
                "analytics": allowed,
                "marketing": False,
                "timestamp": timezone.now().isoformat(),
            }
        )
    )
    return r


def source(r, *, campaign=CAMPAIGN, property_slug="synthetic-ad-apartment"):
    return aa.capture(
        r,
        {"utm_source": "google", "utm_medium": "cpc", "utm_id": campaign},
        property_slug=property_slug,
    )


def code_before_paid():
    r = visitor()
    source(r)
    code = aa.handoff(r)
    reservation, request, attempt = paid()
    binding = aa.bind_admin(reservation.booking_intent.quote, code=code)
    return r, code, binding, reservation, request, attempt


def grant_consent(client):
    client.cookies["lsa_cookie_consent"] = visitor().COOKIES["lsa_cookie_consent"]


def test_first_last_origin_and_campaign_ids_are_coherent_and_text():
    r = visitor()
    source(r)
    source(r, campaign=OTHER_CAMPAIGN, property_slug="another-entry")
    aa.capture(r, {}, property_slug="ordinary-browse")
    data = aa.context(r)
    assert data["first"]["campaign_id"] == CAMPAIGN
    assert data["last"]["campaign_id"] == OTHER_CAMPAIGN
    assert data["origin_property_id"] == "synthetic-ad-apartment"
    assert isinstance(data["last"]["campaign_id"], str)
    assert data["last"]["campaign_evidence"] == "tagged_id"


def test_tagged_home_keeps_unknown_origin_when_later_property_changes():
    r = visitor()
    source(r, property_slug="")
    source(r, campaign=OTHER_CAMPAIGN, property_slug="later-property")
    assert aa.context(r)["origin_captured"] is True
    assert aa.context(r)["origin_property_id"] == ""


@pytest.mark.parametrize(
    "bad", ["966500000000", "1234567890123456789", "{campaignid}", "guest@example.invalid"]
)
def test_unknown_or_sensitive_campaign_value_is_not_retained(bad):
    r = visitor()
    aa.capture(
        r,
        {
            "utm_source": "a-private-name",
            "utm_medium": "guest@example.invalid",
            "utm_id": bad,
            "utm_campaign": "private-guest-name",
            "gclid": "private-click",
        },
    )
    serialized = json.dumps(aa.context(r))
    for forbidden in (
        bad,
        "a-private-name",
        "guest@example.invalid",
        "private-guest-name",
        "private-click",
    ):
        assert forbidden not in serialized
    assert aa.context(r)["first"]["campaign_evidence"] == "click_only"
    assert aa.context(r)["first"]["source"] == "unknown"


def test_denied_and_staff_capture_write_nothing():
    for r in (visitor(allowed=False), visitor(staff=True)):
        assert source(r) is False
        assert aa.handoff(r) == ""
        assert r.session == {}
    assert not AttributionHandoff.objects.exists()


def test_whatsapp_transition_has_no_reference_in_page_or_incoming_url():
    SiteSetting.objects.create(whatsapp_url="https://wa.me/966500000000")
    c = Client()
    grant_consent(c)
    landing = c.get("/ar/", {"utm_source": "google", "utm_medium": "cpc", "utm_id": CAMPAIGN})
    assert landing.status_code == 200
    assert "/contact/whatsapp/?language=ar" in landing.content.decode()
    assert "LSA-" not in landing.content.decode()
    result = c.get(reverse("marketing:whatsapp_transition"), {"language": "ar"})
    assert result.status_code == 302
    target = urlsplit(result.url)
    assert target.hostname == "wa.me"
    message = parse_qs(target.query)["text"][0]
    assert "LSA-" in message
    assert result["Referrer-Policy"] == "no-referrer"
    assert "no-store" in result["Cache-Control"]
    assert AttributionHandoff.objects.count() == 1
    assert not BookingAttribution.objects.exists() and not MarketingEventReceipt.objects.exists()
    # This proves a link transition, not a sent conversation or a paid stay.


def test_admin_explicit_reference_can_close_another_property_without_identity_matching():
    r, code, binding, reservation, _, attempt = code_before_paid()
    fields = aa.purchase_fields(r, attempt, reservation)
    assert fields["attribution_status"] == "reference_linked"
    assert fields["closure_method"] == "admin_whatsapp"
    assert fields["first_campaign_id"] == CAMPAIGN
    assert fields["origin_property_id"] == "synthetic-ad-apartment"
    assert fields["origin_property_id"] != reservation.property.slug
    assert binding.quote_id == reservation.booking_intent.quote_id
    serialized = json.dumps(fields)
    for secret in (
        code,
        binding.journey_hash,
        str(attempt.pk),
        reservation.public_reference,
        reservation.booking_intent.guest_phone,
        reservation.booking_intent.guest_email,
    ):
        assert secret not in serialized


def test_fake_expired_revoked_and_already_used_codes_fail_closed():
    r = visitor()
    source(r)
    code = aa.handoff(r)
    with pytest.raises(ValidationError):
        aa.lookup("LSA-" + "0" * 32)
    row = aa.lookup(code)
    row.expires_at = timezone.now() - timedelta(seconds=1)
    row.save()
    with pytest.raises(ValidationError):
        aa.lookup(code)
    row.expires_at = timezone.now() + timedelta(days=1)
    row.revoked_at = timezone.now()
    row.save()
    with pytest.raises(ValidationError):
        aa.lookup(code)


def test_code_cannot_be_bound_to_a_different_booking_anchor():
    _, code, _, reservation, _, _ = code_before_paid()
    from uuid import uuid4

    from apps.reservations.models import BookingQuote

    other = BookingQuote.objects.get(pk=reservation.booking_intent.quote_id)
    other.pk = uuid4()
    other.save(force_insert=True)
    with pytest.raises(ValidationError, match="طلب آخر"):
        aa.bind_admin(other, code=code)
    assert BookingAttribution.objects.get(quote=reservation.booking_intent.quote).handoff_id


def test_withdrawal_revokes_private_bridge_and_suppresses_future_enrichment():
    r, code, binding, reservation, _, attempt = code_before_paid()
    r.COOKIES["lsa_cookie_consent"] = visitor(allowed=False).COOKIES["lsa_cookie_consent"]
    aa.withdraw(r)
    assert aa.SESSION_KEY not in r.session
    binding.refresh_from_db()
    assert binding.revoked_at
    with pytest.raises(ValidationError):
        aa.lookup(code)
    r.COOKIES["lsa_cookie_consent"] = visitor().COOKIES["lsa_cookie_consent"]
    assert aa.purchase_fields(r, attempt, reservation) == {}


def test_staff_report_is_separate_and_never_becomes_a_captured_campaign():
    reservation, _, attempt = paid()
    binding = aa.bind_admin(reservation.booking_intent.quote, staff_source="google_ads")
    fields = aa.purchase_fields(visitor(), attempt, reservation)
    assert binding.staff_source == "google_ads"
    assert fields["attribution_status"] == "staff_reported"
    assert fields["campaign_evidence"] == "staff_reported"
    assert "first_campaign_id" not in fields and "first_source" not in fields


def test_clean_guest_receipt_enriches_only_existing_paid_event_and_acknowledges_same_order():
    _, code, _, reservation, _, attempt = code_before_paid()
    c = Client()
    grant_consent(c)
    assert callback(c, attempt).status_code == 302
    page = c.get("/payments/receipt/", secure=True)
    event = page.context["purchase_event"]
    assert event["attribution_status"] == "reference_linked"
    assert event["value"] == attempt.amount
    assert "data-aa-first-campaign-id" in page.content.decode()
    assert code not in page.content.decode()
    prepared, receipt = prepare_purchase_event(
        payment_attempt=attempt, reservation=reservation, request=visitor()
    )
    assert prepared["transaction_id"] == event["transaction_id"]
    assert prepared["value"] == event["value"] and prepared["currency"] == event["currency"]
    assert MarketingEventReceipt.objects.count() == 1
    token = page.context["purchase_receipt_token"]
    assert (
        c.post(reverse("marketing:purchase_acknowledge"), {"receipt_token": token}).status_code
        == 204
    )
    assert (
        prepare_purchase_event(payment_attempt=attempt, reservation=reservation, request=visitor())[
            0
        ]
        is None
    )
    assert receipt_for(attempt).status == "emitted"


@pytest.mark.parametrize("legacy_status", ["prepared", "emitted"])
def test_existing_reservation_receipt_is_reused_instead_of_historical_new_identity(legacy_status):
    reservation, _, attempt = paid()
    legacy = MarketingEventReceipt.objects.create(
        event_name="purchase",
        object_type="Reservation",
        object_reference_hash=MarketingEventReceipt.reference_hmac(reservation.public_reference),
        status=legacy_status,
    )
    assert receipt_for(attempt).pk == legacy.pk
    event, receipt = prepare_purchase_event(payment_attempt=attempt, reservation=reservation)
    assert receipt.pk == legacy.pk
    assert (event is None) == (legacy_status == "emitted")
    assert MarketingEventReceipt.objects.count() == 1


def test_partial_capture_is_not_classified_as_new_acquisition_and_routes_keep_amount():
    reservation, _, attempt = paid(amount="500")
    fields = aa.purchase_fields(visitor(), attempt, reservation)
    assert fields["acquisition_kind"] == "partial_payment"
    event, _ = prepare_purchase_event(
        payment_attempt=attempt, reservation=reservation, request=visitor()
    )
    assert event["value"] == 500 and event["currency"] == "SAR"


def test_staff_browser_cannot_create_or_dispatch_guest_purchase():
    _, _, _, reservation, _, attempt = code_before_paid()
    assert prepare_purchase_event(
        payment_attempt=attempt, reservation=reservation, request=visitor(staff=True)
    ) == (None, None)
    assert redirect_verified_purchase(visitor(staff=True), attempt) is None
    assert not MarketingEventReceipt.objects.exists()


@pytest.mark.parametrize(
    "overrides",
    [
        {"first_source": "private-name"},
        {"last_medium": "a-phone-number"},
        {"first_campaign_id": "966500000000"},
        {"last_campaign_id": 24262302633},
        {"closure_method": "staff-session"},
    ],
)
def test_purchase_enrichment_rejects_uncontrolled_values(overrides):
    with pytest.raises(ValueError):
        sanitize_analytics_event("purchase", {"attribution_version": "aa1", **overrides})
    assert len(EVENT_SCHEMAS["purchase"]) == 25


@pytest.mark.parametrize(
    "status,request_status", [("pending", "open"), ("failed", "open"), ("succeeded", "open")]
)
def test_incomplete_payment_does_not_prepare_a_purchase(status, request_status):
    reservation, _, attempt = paid(status=status, request_status=request_status)
    assert prepare_purchase_event(
        payment_attempt=attempt, reservation=reservation, request=visitor()
    ) == (None, None)
    assert redirect_verified_purchase(visitor(), attempt) is None
    assert not MarketingEventReceipt.objects.exists()


def test_late_consent_is_same_origin_csrf_protected_and_staff_never_captures():
    c = Client(enforce_csrf_checks=True)
    grant_consent(c)
    response = c.post(
        reverse("marketing:attribution_consent"), {"page_kind": "home", "utm_id": CAMPAIGN}
    )
    assert response.status_code == 403
    assert aa.SESSION_KEY not in c.session


def test_denied_purchase_retains_legacy_anonymous_policy_but_no_source_fields():
    _, _, _, reservation, _, attempt = code_before_paid()
    assert aa.purchase_fields(visitor(allowed=False), attempt, reservation) == {}


def test_private_source_never_changes_marrakech_into_riyadh():
    r, _, _, reservation, _, attempt = code_before_paid()
    reservation.property.city_en = "Marrakech"
    reservation.property.city = "Marrakech"
    reservation.property.city_ar = "مراكش"
    assert aa.purchase_fields(r, attempt, reservation)["market"] == "marrakech"


def test_morning_creation_persists_private_reference_without_a_purchase(settings):
    from decimal import Decimal
    from uuid import uuid4

    from apps.reservations.morning_stays import create_stay
    from tests.test_manual_booking_operations_phase61 import owner
    from tests.test_morning_stays import property_and_remote, window

    settings.MORNING07_N8N_ENABLED = False
    settings.MORNING07_PAYMENT_HANDOFF_ENABLED = False
    r = visitor()
    source(r)
    code = aa.handoff(r)
    p, remote = property_and_remote()
    start, end = window()
    stay = create_stay(
        actor=owner(),
        request_id=uuid4(),
        property_obj=p,
        starts_at=start,
        ends_at=end,
        guest_name="Synthetic Guest",
        guest_phone="+16502530000",
        guest_email="synthetic@example.invalid",
        guests=1,
        amount_sar=Decimal("500"),
        client=remote,
        attribution_reference=code,
    )
    binding = BookingAttribution.objects.get(morning_stay=stay)
    assert binding.linkage == "reference_linked"
    assert binding.quote_id is None and binding.day_use_stay_id is None
    assert not MarketingEventReceipt.objects.exists()


def test_daytime_creation_persists_private_reference_without_a_purchase(day_use_rig):  # noqa: F811
    from apps.reservations.day_use_stays import create_stay

    r = visitor()
    source(r)
    code = aa.handoff(r)
    stay = create_stay(**day_use_rig.values, attribution_reference=code)
    binding = BookingAttribution.objects.get(day_use_stay=stay)
    assert (
        binding.linkage == "reference_linked"
        and binding.snapshot["first"]["campaign_id"] == CAMPAIGN
    )
    assert not MarketingEventReceipt.objects.exists()


@pytest.mark.parametrize("method", ["cash", "bank_transfer"])
def test_private_source_remains_joined_to_real_manager_receipt_without_staff_purchase(
    method, settings
):
    from apps.payments.offline_receipts import receipt_snapshot, record_offline_receipt
    from tests.test_manual_booking_operations_phase61 import owner
    from tests.test_reservation_payment_links import manual_stay

    settings.MANUAL_PAYMENT_RECEIPTS_ENABLED = True
    r = visitor()
    source(r)
    code = aa.handoff(r)
    reservation = manual_stay()
    reservation.is_test = False
    reservation.save()
    actor = owner()
    binding = aa.bind_admin(reservation.booking_intent.quote, code=code, actor=actor)
    staff = RequestFactory().post("/admin/")
    staff.user = actor
    receipt = record_offline_receipt(
        request=staff,
        reservation_id=reservation.pk,
        amount=reservation.total_price,
        currency=reservation.currency,
        method=method,
        reference="synthetic-receipt-only",
        received_at=timezone.now() - timedelta(minutes=1),
        snapshot_digest=receipt_snapshot(reservation),
        confirmed=True,
    )
    reservation.refresh_from_db()
    assert receipt.reservation_id == binding.quote.booking_intent.reservation.pk
    assert reservation.payment_status == "paid"
    assert binding.summary["first_campaign"] == "البحث العربي"
    assert prepare_purchase_event(
        payment_attempt=receipt.payment_attempt, reservation=reservation, request=staff
    ) == (None, None)
    assert not MarketingEventReceipt.objects.exists()


def test_repriced_unconsumed_quote_keeps_exact_source_and_handoff_identity():
    from uuid import uuid4

    from apps.reservations.models import BookingQuote

    reservation, _, _ = paid()
    old = BookingQuote.objects.get(pk=reservation.booking_intent.quote_id)
    old.pk = uuid4()
    old.save(force_insert=True)
    r = visitor()
    source(r)
    aa.bind_session(r, old)
    binding = BookingAttribution.objects.get(quote=old)
    new = BookingQuote.objects.get(pk=old.pk)
    new.pk = uuid4()
    new.save(force_insert=True)
    aa.replace_quote_binding(old, new)
    binding.refresh_from_db()
    assert binding.quote_id == new.pk and binding.snapshot["first"]["campaign_id"] == CAMPAIGN
    assert not BookingAttribution.objects.filter(quote=old).exists()


def test_private_summary_is_readable_and_never_exposes_reference_or_guest_data():
    from django.template.loader import render_to_string

    _, code, binding, reservation, _, _ = code_before_paid()
    html = render_to_string(
        "admin/reservations/includes/attribution_summary.html", {"source": binding}
    )
    assert "مصدر الحجز" in html and "البحث العربي" in html and "مرتبط بمرجع واتساب" in html
    for secret in (
        code,
        binding.journey_hash,
        reservation.public_reference,
        reservation.booking_intent.guest_email,
        reservation.booking_intent.guest_phone,
    ):
        assert secret not in html


def test_frozen_contract_fields_vocab_and_catalogue_match_source():
    from pathlib import Path

    contract = json.loads(
        (Path(__file__).parents[1] / "docs/assisted-attribution-contract.json").read_text(
            encoding="utf-8-sig"
        )
    )
    assert set(contract["events"]["purchase"]["addedParameters"]) == set(aa.FIELDS)
    assert set(contract["enums"]["first_source"]) == aa.SOURCES
    assert set(contract["enums"]["last_medium"]) == aa.MEDIUMS
    assert set(contract["campaignCatalogue"]["ids"]) == aa.KNOWN_CAMPAIGNS
    assert set(contract["enums"]["acquisition_kind"]) == aa.ENUMS["acquisition_kind"]
