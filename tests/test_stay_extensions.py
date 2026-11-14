"""Synthetic provider/collection tests. No real guest or outgoing provider request."""

import os
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from django.contrib.auth import get_user_model
from django.core import signing
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from apps.integrations.hostaway.exceptions import HostawayNetworkError
from apps.integrations.hostaway.reservation_validators import (
    HostawayReservationCreateResult,
    validate_reservation_response,
)
from apps.integrations.hostaway.webhook_processor import sync_reservation_snapshot
from apps.payments.models import PaymentAttempt
from apps.reservations.manual_bookings import (
    create_manual_booking_in_hostaway,
    finalize_manual_booking_draft,
    recheck_manual_booking_draft,
)
from apps.reservations.models import (
    Reservation,
    StayExtension,
    StayExtensionChain,
    StayExtensionEvent,
)
from apps.reservations.services.hostaway_booking import HostawayBookingService
from apps.reservations.stay_extension_communications import (
    SALT,
    acknowledge_ownership,
    capability,
    deliver_events,
)
from apps.reservations.stay_extensions import (
    chain_context,
    refresh_chain,
    start_extension,
)
from tests.test_booking_models_services import make_availability, make_property
from tests.test_manual_booking_operations_phase61 import FakeAvailabilityService
from tests.test_revised_payment_flow import _standalone

pytestmark = pytest.mark.django_db


@pytest.fixture
def setup_extension(settings, monkeypatch):
    settings.OPERATIONS_OWNER_ENFORCEMENT_ENABLED = True
    settings.OPERATIONS_OWNER_EMAIL = "synthetic-owner@example.invalid"
    settings.HOSTAWAY_LIVE_BOOKING_ENABLED = True
    settings.HOSTAWAY_DIRECT_CHANNEL_ID = 2000
    monkeypatch.setattr("apps.reservations.tasks.deliver_stay_extension_events.delay", Mock())
    property_obj = make_property()
    property_obj.hostaway_listing_map_id = 8001
    property_obj.hostaway_is_active = True
    property_obj.time_zone_name = "Asia/Riyadh"
    property_obj.display_check_in_hour = 15
    property_obj.display_check_out_hour = 12
    property_obj.save()
    actor = get_user_model().objects.create_superuser(
        username="synthetic-extension-owner",
        email=settings.OPERATIONS_OWNER_EMAIL,
        password="unused-test-password",
    )
    today = timezone.localdate()
    raw = {
        "id": 91001,
        "listingMapId": 8001,
        "channelId": 2002,
        "channelName": "Booking.com",
        "status": "new",
        "arrivalDate": (today - timedelta(days=2)).isoformat(),
        "departureDate": (today + timedelta(days=1)).isoformat(),
        "numberOfGuests": 2,
        "currency": "SAR",
        "totalPrice": 1500,
        "guestFirstName": "ضيف",
        "guestLastName": "مصطنع",
        "guestEmail": "synthetic@example.invalid",
        "phone": "+966500000000",
        "guestLocale": "ar",
        "sensitive_unused": "must-never-be-stored",
    }
    available = make_availability(property_obj)
    quote = replace(
        available.quote, check_in=today + timedelta(days=1), check_out=today + timedelta(days=3)
    )
    available = replace(available, quote=quote)
    availability = FakeAvailabilityService(available)
    extension = start_extension(
        raw=raw, check_out=quote.check_out, guests=2, actor=actor, availability_service=availability
    )
    client = Client()
    client.force_login(actor)

    def current_source(reservation_id, **kwargs):
        if reservation_id == raw["id"]:
            return dict(raw)
        reservation = Reservation.objects.get(hostaway_reservation_id=reservation_id)
        return {
            **raw,
            "id": reservation_id,
            "channelId": reservation.channel_id,
            "arrivalDate": reservation.check_in.isoformat(),
            "departureDate": reservation.check_out.isoformat(),
            "totalPrice": str(reservation.total_price),
            "currency": reservation.currency,
            "status": reservation.normalized_status,
        }

    monkeypatch.setattr(
        "apps.reservations.stay_extension_communications.read_source", current_source
    )
    return SimpleNamespace(
        extension=extension,
        actor=actor,
        raw=raw,
        available=available,
        availability=availability,
        property=property_obj,
        client=client,
    )


def finalize(case):
    case.extension.draft.refresh_from_db()
    if case.extension.draft.status == "ready_for_payment":
        return
    result = finalize_manual_booking_draft(
        draft_id=case.extension.draft_id,
        final_total_price=Decimal("500.25"),
        guest_data={
            "guest_first_name": "ضيف",
            "guest_last_name": "مصحح",
            "guest_email": "corrected@example.invalid",
            "guest_phone": "+966500000000",
            "guest_language": "ar",
            "billing_address": {
                "billing_street1": "10 Test Road",
                "billing_city": "Riyadh",
                "billing_state": "Riyadh",
                "billing_country": "SA",
                "billing_postcode": "12345",
            },
        },
    )
    assert result.code == "ready_for_payment"


class FakeHostaway:
    def __init__(self, case, *, unknown=False, early=False):
        self.case, self.unknown, self.early = case, unknown, early
        self.posts = []

    def get_reservation_invoice_data(self, reservation_id):
        assert reservation_id == self.case.raw["id"]
        return {"status": "success", "result": dict(self.case.raw)}

    def create_reservation_with_price_details(self, request):
        self.posts.append(request.to_payload())
        if self.unknown:
            raise HostawayNetworkError("synthetic-uncertain")
        raw = {
            **self.case.raw,
            **request.to_payload(),
            "id": getattr(self.case, "new_id", 92001),
            "status": "new",
        }
        snapshot = validate_reservation_response({"status": "success", "result": raw})
        if self.early:
            reservation, _, _ = sync_reservation_snapshot(snapshot)
            self.case.extension.refresh_from_db()
            assert reservation.pk == self.case.extension.reservation_id
        return HostawayReservationCreateResult(snapshot)


def create_child(case, monkeypatch, *, unknown=False, early=False):
    finalize(case)

    def ack(extension_id, **kwargs):
        StayExtension.objects.filter(pk=extension_id).update(ownership_acknowledged=True)
        return True

    monkeypatch.setattr(
        "apps.reservations.stay_extension_communications.acknowledge_ownership", ack
    )
    provider = FakeHostaway(case, unknown=unknown, early=early)
    service = HostawayBookingService(client=provider, availability_service=case.availability)
    result = create_manual_booking_in_hostaway(
        draft_id=case.extension.draft_id, booking_service=service
    )
    case.extension.refresh_from_db()
    return result, provider, service


def pay(case):
    reservation = case.extension.reservation
    reservation.payment_status = "paid"
    reservation.save(update_fields=["payment_status"])
    PaymentAttempt.objects.create(
        booking_intent=reservation.booking_intent,
        provider="manual",
        provider_reference="synthetic-proof",
        provider_payment_id="synthetic:" + str(reservation.pk),
        idempotency_key="proof:" + str(reservation.pk),
        amount=reservation.total_price,
        currency=reservation.currency,
        status="succeeded",
        verified_at=timezone.now(),
    )
    refresh_chain(case.extension.chain_id)
    case.extension.refresh_from_db()


def test_ota_source_is_not_imported_or_modified_and_draft_is_private(setup_extension):
    case = setup_extension
    assert Reservation.objects.count() == 0
    assert case.extension.draft.check_in.isoformat() == case.raw["departureDate"]
    assert case.extension.chain.root_snapshot["channel_id"] == 2002
    assert "sensitive_unused" not in str(case.extension.chain.root_snapshot)
    assert "guestEmail" not in str(case.extension.previous_snapshot)
    duplicate = start_extension(
        raw=case.raw,
        check_out=case.available.quote.check_out,
        guests=2,
        actor=case.actor,
        availability_service=case.availability,
    )
    assert duplicate.pk == case.extension.pk
    assert StayExtension.objects.count() == 1


@pytest.mark.parametrize("early", [False, True])
def test_create_is_one_independent_unpaid_booking_even_with_early_webhook(
    setup_extension, monkeypatch, early
):
    case = setup_extension
    result, provider, service = create_child(case, monkeypatch, early=early)
    assert result.code == "created"
    assert len(provider.posts) == 1
    assert provider.posts[0]["channelId"] == 2000
    assert provider.posts[0]["hostNote"] == "EXT01:" + str(case.extension.pk)
    assert provider.posts[0]["guestLastName"] == "مصحح"
    assert case.raw["channelId"] == 2002 and case.raw["guestLastName"] == "مصطنع"
    assert Reservation.objects.count() == 1
    assert result.reservation.payment_status != "paid"
    assert case.extension.state == "created_unpaid"
    assert (
        create_manual_booking_in_hostaway(
            draft_id=case.extension.draft_id, booking_service=service
        ).code
        == "already_created"
    )
    assert len(provider.posts) == 1
    state = chain_context(case.extension.chain)
    assert state["terminal_hostaway_reservation_id"] == 91001
    assert state["chain_members"][0]["allow_checkout"] is True
    assert state["chain_members"][1]["allow_checkout"] is False
    assert (
        recheck_manual_booking_draft(draft_id=case.extension.draft_id, actor=case.actor).code
        == "not_recheckable"
    )


def test_verified_collection_alone_activates_and_duplicate_refresh_does_not_repeat(
    setup_extension, monkeypatch
):
    case = setup_extension
    create_child(case, monkeypatch)
    reservation = case.extension.reservation
    reservation.payment_status = "paid"
    reservation.save(update_fields=["payment_status"])
    refresh_chain(case.extension.chain_id)
    case.extension.refresh_from_db()
    assert case.extension.state == "created_unpaid"  # A paid label is not proof.
    pay(case)
    assert case.extension.state == "active"
    assert case.extension.activation_generation == 1
    state = chain_context(case.extension.chain)
    assert state["terminal_hostaway_reservation_id"] == 92001
    assert state["chain_members"][0]["allow_checkout"] is False
    assert (
        state["chain_members"][0]["review_anchor_date"]
        == case.extension.draft.check_out.isoformat()
    )
    count = StayExtensionEvent.objects.count()
    refresh_chain(case.extension.chain_id)
    assert StayExtensionEvent.objects.count() == count


def test_uncertain_provider_create_cannot_be_replayed(setup_extension, monkeypatch):
    case = setup_extension
    _, provider, service = create_child(case, monkeypatch, unknown=True)
    create_manual_booking_in_hostaway(draft_id=case.extension.draft_id, booking_service=service)
    assert len(provider.posts) == 1


@pytest.mark.parametrize("environment", ["test", "production"])
def test_online_payment_requires_exact_production_collection(
    setup_extension, monkeypatch, settings, environment
):
    from apps.payments.payment_links import complete_request_payment, issue_request

    case = setup_extension
    settings.RESERVATION_PAYMENT_LINKS_ENABLED = True
    settings.HYPERPAY_ENVIRONMENT = environment
    create_child(case, monkeypatch)
    reservation = case.extension.reservation
    request = issue_request(reservation.pk)
    attempt = PaymentAttempt.objects.create(
        booking_intent=reservation.booking_intent,
        payment_request=request,
        provider="hyperpay",
        provider_reference="synthetic-online",
        provider_payment_id="synthetic-online",
        idempotency_key="synthetic-online",
        amount=request.amount_sar,
        currency="SAR",
        status="succeeded",
        verified_at=timezone.now(),
    )
    complete_request_payment(attempt)
    refresh_chain(case.extension.chain_id)
    case.extension.refresh_from_db()
    assert case.extension.state == ("active" if environment == "production" else "created_unpaid")


def test_wrong_ownership_ack_never_sends_provider_post(setup_extension, monkeypatch):
    case = setup_extension
    finalize(case)
    monkeypatch.setattr(
        "apps.reservations.stay_extension_communications.acknowledge_ownership",
        lambda *args, **kwargs: False,
    )
    provider = FakeHostaway(case)
    service = HostawayBookingService(client=provider, availability_service=case.availability)
    result = create_manual_booking_in_hostaway(
        draft_id=case.extension.draft_id, booking_service=service
    )
    assert result.code == "extension_ownership_requires_review"
    assert provider.posts == []
    assert PaymentAttempt.objects.count() == 0


def test_parent_changed_blocks_before_ownership_or_provider_write(setup_extension, monkeypatch):
    case = setup_extension
    finalize(case)
    provider = FakeHostaway(case)
    case.raw["departureDate"] = case.available.quote.check_out.isoformat()
    service = HostawayBookingService(client=provider, availability_service=case.availability)
    result = create_manual_booking_in_hostaway(
        draft_id=case.extension.draft_id, booking_service=service
    )
    assert result.code == "extension_ownership_requires_review" and provider.posts == []


def test_cancelled_child_restores_original_and_changed_active_dates_require_review(
    setup_extension, monkeypatch
):
    case = setup_extension
    create_child(case, monkeypatch)
    pay(case)
    reservation = case.extension.reservation
    reservation.check_out += timedelta(days=1)
    reservation.save()
    refresh_chain(case.extension.chain_id)
    assert (
        chain_context(StayExtensionChain.objects.get(pk=case.extension.chain_id))["chain_state"]
        == "review"
    )
    reservation.normalized_status = "cancelled"
    reservation.save()
    refresh_chain(case.extension.chain_id)
    state = chain_context(StayExtensionChain.objects.get(pk=case.extension.chain_id))
    assert state["terminal_hostaway_reservation_id"] == 91001
    assert state["chain_state"] == "original"


def test_context_is_scoped_latest_read_only_and_fail_closed(setup_extension, monkeypatch):
    case = setup_extension
    token = capability(case.extension)
    url = reverse("reservations:stay_extension_context", args=[case.extension.pk])
    assert case.client.get(url).status_code == 404
    assert case.client.get(url, HTTP_X_LSA_EXTENSION_TOKEN=token + "x").status_code == 404
    expired = signing.dumps({"extension_id": str(case.extension.pk), "expires": 1}, salt=SALT)
    assert case.client.get(url, HTTP_X_LSA_EXTENSION_TOKEN=expired).status_code == 404
    monkeypatch.setattr(
        "apps.reservations.stay_extension_communications.read_source",
        lambda *args, **kwargs: dict(case.raw),
    )
    case.extension.chain.revision = 4
    case.extension.chain.save()
    response = case.client.get(url, HTTP_X_LSA_EXTENSION_TOKEN=token)
    assert response.status_code == 200 and response.json()["revision"] == 4
    assert response.json()["communication_ready"] is True
    assert "billing_address" not in response.json() and "guest_email" not in response.json()
    assert "no-store" in response["Cache-Control"]
    case.raw["status"] = "cancelled"
    response = case.client.get(url, HTTP_X_LSA_EXTENSION_TOKEN=token)
    assert response.json()["communication_ready"] is False
    assert response.json()["chain_members"][0]["allow_checkout"] is False


@pytest.mark.parametrize("cancel_middle", [True, False])
def test_chain_never_uses_maximum_checkout_across_cancelled_gap(
    setup_extension, monkeypatch, cancel_middle
):
    first = setup_extension
    _, provider, _ = create_child(first, monkeypatch)
    pay(first)
    original_now = timezone.now()
    monkeypatch.setattr(
        "apps.reservations.stay_extensions.timezone.now", lambda: original_now + timedelta(days=1)
    )
    raw = {**first.raw, **provider.posts[0], "id": 92001, "status": "new", "channelName": "Direct"}
    available = replace(
        first.available,
        quote=replace(
            first.available.quote,
            check_in=first.available.quote.check_out,
            check_out=first.available.quote.check_out + timedelta(days=2),
        ),
    )
    availability = FakeAvailabilityService(available)
    extension = start_extension(
        raw=raw,
        check_out=available.quote.check_out,
        guests=2,
        actor=first.actor,
        availability_service=availability,
    )
    second = SimpleNamespace(
        extension=extension, raw=raw, available=available, availability=availability, new_id=93001
    )
    create_child(second, monkeypatch)
    pay(second)
    target = first if cancel_middle else second
    reservation = target.extension.reservation
    reservation.normalized_status = "cancelled"
    reservation.save()
    refresh_chain(extension.chain_id)
    state = chain_context(StayExtensionChain.objects.get(pk=extension.chain_id))
    if cancel_middle:
        assert state["chain_state"] == "review"
        assert not any(member["allow_checkout"] for member in state["chain_members"])
    else:
        assert state["chain_state"] == "active"
        assert state["terminal_hostaway_reservation_id"] == 92001


class FakeTransport:
    def __init__(self, case, *, wrong=False):
        self.case, self.wrong, self.posts = case, wrong, []
        self.delivery_pending = False

    def get(self, url):
        return SimpleNamespace(
            raise_for_status=lambda: None,
            json=lambda: {"contract_version": 1, "build_ready": True, "receive_enabled": True},
        )

    def post(self, url, *, json):
        self.posts.append(json)
        return SimpleNamespace(
            raise_for_status=lambda: None,
            json=lambda: {
                "code": "extension_ownership_persisted",
                "extension_id": str(self.case.extension.pk),
                "revision": -1 if self.wrong else json["revision"],
                "contract_version": 1,
                "accepted": True,
                "delivery_pending": self.delivery_pending,
                "event_id": json["event_id"],
            },
        )


def test_exact_ack_and_event_capability_prove_event_identity(setup_extension, monkeypatch):
    case = setup_extension
    transport = FakeTransport(case, wrong=True)
    assert acknowledge_ownership(case.extension.pk, transport=transport) is False
    transport.wrong = False
    assert acknowledge_ownership(case.extension.pk, transport=transport) is True
    refresh_chain(case.extension.chain_id)
    assert deliver_events(case.extension.pk, transport=transport) == {"accepted": 1}
    assert deliver_events(case.extension.pk, transport=transport) == {"accepted": 0}
    envelope = transport.posts[-1]
    monkeypatch.setattr(
        "apps.reservations.stay_extension_communications.read_source",
        lambda *args, **kwargs: dict(case.raw),
    )
    url = reverse("reservations:stay_extension_context", args=[case.extension.pk])
    response = case.client.get(url, HTTP_X_LSA_EXTENSION_TOKEN=envelope["event_token"])
    assert response.json()["source_event_id"] == envelope["event_id"]
    assert response.json()["source_event_revision"] == envelope["revision"]
    assert response.json()["capability_purpose"] == "event"


def test_received_event_remains_pending_until_immediate_chain_delivery_finishes(setup_extension):
    case = setup_extension
    transport = FakeTransport(case)
    assert acknowledge_ownership(case.extension.pk, transport=transport)
    refresh_chain(case.extension.chain_id)
    transport.delivery_pending = True
    assert deliver_events(case.extension.pk, transport=transport) == {"accepted": 0}
    event_id = transport.posts[-1]["event_id"]
    assert StayExtensionEvent.objects.get(pk=event_id).state == "pending"
    transport.delivery_pending = False
    assert deliver_events(case.extension.pk, transport=transport) == {"accepted": 1}
    assert transport.posts[-1]["event_id"] == event_id


def test_source_pages_include_ota_and_advance_even_if_filtered_page_is_empty(
    setup_extension, monkeypatch
):
    case = setup_extension
    provider = Mock()
    provider.__enter__ = Mock(return_value=provider)
    provider.__exit__ = Mock(return_value=False)
    provider.guest_stay_page.return_value = [dict(case.raw)]
    monkeypatch.setattr("apps.reservations.stay_extension_views.HostawayClient", lambda: provider)
    url = reverse("notifications:stay_extension_sources")
    page = case.client.get(url, {"channel": "بوكنق"})
    assert page.status_code == 200 and "Booking.com" in page.content.decode()
    assert page.context["next_url"]
    empty = case.client.get(url, {"q": "غير موجود"})
    assert not empty.context["rows"] and empty.context["next_url"]
    case.client.get(url + empty.context["next_url"])
    assert provider.guest_stay_page.call_args.kwargs["after_id"] == case.raw["id"]
    outsider = Client()
    assert outsider.get(url).status_code == 302


def test_generic_draft_url_cannot_bypass_extension_review(setup_extension):
    case = setup_extension
    response = case.client.post(
        reverse("notifications:manual_booking_detail", args=[case.extension.draft_id]),
        {"action": "create_hostaway_and_request_payment"},
    )
    assert response.status_code == 302
    assert response.url == reverse("notifications:stay_extension_detail", args=[case.extension.pk])


@pytest.mark.parametrize("marker", ["MorningStay synthetic", "DU08:synthetic"])
def test_morning_and_day_use_sources_keep_their_independent_journey(setup_extension, marker):
    from apps.reservations.stay_extensions import eligible_source

    with pytest.raises(ValueError, match="special_stay"):
        eligible_source({**setup_extension.raw, "hostNote": marker})


def test_collection_survives_projection_failure_and_recovers(setup_extension, monkeypatch):
    from apps.reservations.stay_extensions import capture_reservation_change, recover_pending

    case = setup_extension
    create_child(case, monkeypatch)
    reservation = case.extension.reservation
    reservation.payment_status = "paid"
    reservation.save()
    original = refresh_chain
    monkeypatch.setattr(
        "apps.reservations.stay_extensions.refresh_chain",
        Mock(side_effect=RuntimeError("synthetic")),
    )
    capture_reservation_change(reservation.pk)
    reservation.refresh_from_db()
    assert reservation.payment_status == "paid"
    monkeypatch.setattr("apps.reservations.stay_extensions.refresh_chain", original)
    # A verified collection, not the paid label alone, is still required.
    pay(case)
    monkeypatch.setattr("apps.reservations.stay_extension_communications.deliver_events", Mock())
    recover_pending()
    assert case.extension.state == "active"


@pytest.mark.django_db(transaction=True)
def test_two_managers_cannot_create_two_children_for_same_parent(setup_extension):
    from concurrent.futures import ThreadPoolExecutor

    from django.db import close_old_connections, connection

    if connection.vendor != "postgresql":
        pytest.skip("Production row-lock behavior requires PostgreSQL")
    case = setup_extension
    case.extension.delete()
    count = len(case.availability.requests)

    def start():
        close_old_connections()
        try:
            return start_extension(
                raw=case.raw,
                check_out=case.available.quote.check_out,
                guests=2,
                actor=case.actor,
                availability_service=case.availability,
            ).pk
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: start(), range(2)))
    assert results[0] == results[1]
    assert StayExtension.objects.count() == 1
    assert len(case.availability.requests) == count + 1


def test_render_extension_layout(setup_extension, monkeypatch, tmp_path):
    case = setup_extension
    monkeypatch.setattr(
        "apps.reservations.stay_extension_views.read_source", lambda *args, **kwargs: dict(case.raw)
    )
    provider = Mock()
    provider.__enter__ = Mock(return_value=provider)
    provider.__exit__ = Mock(return_value=False)
    provider.guest_stay_page.return_value = [dict(case.raw)]
    monkeypatch.setattr("apps.reservations.stay_extension_views.HostawayClient", lambda: provider)
    output = Path(os.environ.get("LSA_CONTACT_VISUAL_DIR", tmp_path))
    output.mkdir(parents=True, exist_ok=True)
    sources = reverse("notifications:stay_extension_sources")
    detail = reverse("notifications:stay_extension_detail", args=[case.extension.pk])
    for name, url in [
        ("sources", sources),
        ("selection", sources + "?source=91001"),
        ("guest", detail),
    ]:
        response = case.client.get(url)
        assert response.status_code == 200
        (output / f"extension-{name}.html").write_text(_standalone(response.content.decode()))
    finalize(case)
    response = case.client.get(detail)
    assert response.status_code == 200
    (output / "extension-review.html").write_text(_standalone(response.content.decode()))
    create_child(case, monkeypatch)
    response = case.client.get(detail)
    assert response.status_code == 200
    (output / "extension-unpaid.html").write_text(_standalone(response.content.decode()))
