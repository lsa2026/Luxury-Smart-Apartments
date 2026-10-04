import hashlib
import uuid
from dataclasses import replace
from types import SimpleNamespace

import pytest
from django.contrib.auth import get_user_model
from django.test import Client, override_settings

from apps.notifications.models import AuditLog
from apps.reservations import sama_booking_api as api
from apps.reservations.manual_bookings import (
    create_manual_booking_draft,
    create_manual_booking_in_hostaway,
)
from apps.reservations.models import ManualBookingDraft, Reservation
from apps.reservations.operations_forms import ManualBookingAvailabilityForm
from tests.test_booking_models_services import make_availability, make_property
from tests.test_manual_booking_operations_phase61 import (
    FakeAvailabilityService,
    FakeManualHostawayBookingService,
)

pytestmark = pytest.mark.django_db
TOKEN = "ab" * 32
HEADERS = {"HTTP_AUTHORIZATION": "Bearer " + TOKEN}
ENABLED = override_settings(
    SAMA_BOOKING_ENABLED=True, SAMA_BOOKING_KEY_SHA256=hashlib.sha256(TOKEN.encode()).hexdigest()
)


def post(path, data):
    return Client(enforce_csrf_checks=True).post(
        path, data=data, content_type="application/json", secure=True, **HEADERS
    )


def prepared(monkeypatch, guests=3):
    property_obj = make_property()
    available = make_availability(property_obj)
    available = replace(available, quote=replace(available.quote, guests=guests))
    captured = []

    def fake_create(**kwargs):
        captured.append(kwargs)
        return create_manual_booking_draft(
            **kwargs, availability_service=FakeAvailabilityService(available)
        )

    monkeypatch.setattr(api, "create_manual_booking_draft", fake_create)
    data = {
        "request_id": str(uuid.uuid4()),
        "property_slug": property_obj.slug,
        "check_in": available.quote.check_in.isoformat(),
        "check_out": available.quote.check_out.isoformat(),
        "guests": guests,
    }
    result = post("/reservations/sama/prepare/", data)
    assert result.status_code == 200, result.json()
    return data, result.json()["booking"], captured


def guest(quote):
    return {
        "guest_first_name": "Synthetic",
        "guest_last_name": "Guest",
        "guest_email": "guest@example.invalid",
        "guest_phone": "+966567528594",
        "accepted_total": quote["total"],
        "currency": quote["currency"],
        "terms_accepted": True,
    }


@ENABLED
def test_api_requires_bearer_and_https_even_with_session_login():
    client = Client()
    path = "/reservations/sama/health/"
    assert client.get(path, secure=True).status_code == 401
    assert client.get(path, **HEADERS).status_code == 403
    assert (
        client.get(path, secure=True, HTTP_AUTHORIZATION="Bearer " + "cd" * 32).status_code == 401
    )
    assert client.get(path, secure=True, **HEADERS).json()["code"] == "ready"
    assert client.post(path, secure=True, **HEADERS).status_code == 405
    assert get_user_model().objects.count() == 0


def test_api_disabled_by_default():
    assert Client().get("/reservations/sama/health/", secure=True, **HEADERS).status_code == 503


@ENABLED
def test_prepare_preserves_guests_and_idempotency_without_booking_or_messaging(monkeypatch):
    data, quote, captured = prepared(monkeypatch)
    assert quote["guests"] == captured[0]["guests"] == 3
    assert Reservation.objects.count() == 0
    assert post("/reservations/sama/prepare/", data).json()["booking"] == quote
    assert len(captured) == 1 and ManualBookingDraft.objects.count() == 1
    changed = {**data, "guests": 2}
    assert post("/reservations/sama/prepare/", changed).status_code == 409
    user = get_user_model().objects.get(username=api.ACTOR)
    assert not user.is_staff and not user.is_superuser and not user.has_usable_password()
    audit = AuditLog.objects.get(action="sama.booking_prepared")
    assert audit.actor_user == user and "guest@example.invalid" not in audit.summary


@ENABLED
@pytest.mark.parametrize("guests", [True, 0, -1, 21, "2"])
def test_invalid_guest_count_does_not_call_provider(monkeypatch, guests):
    def never(**kwargs):
        raise AssertionError("Provider must not be called")

    monkeypatch.setattr(api, "create_manual_booking_draft", never)
    data = {
        "request_id": str(uuid.uuid4()),
        "property_slug": "unknown",
        "check_in": "2026-11-10",
        "check_out": "2026-11-12",
        "guests": guests,
    }
    assert post("/reservations/sama/prepare/", data).status_code == 400


@ENABLED
def test_confirm_creates_once_and_requests_existing_accounting_once(monkeypatch):
    data, quote, _ = prepared(monkeypatch)
    creates, sends = [], []

    def create(*, draft_id):
        creates.append(draft_id)
        return create_manual_booking_in_hostaway(
            draft_id=draft_id, booking_service=FakeManualHostawayBookingService()
        )

    def send(*, reservation_id):
        sends.append(reservation_id)
        return SimpleNamespace(code="sent")

    monkeypatch.setattr(api, "create_manual_booking_in_hostaway", create)
    monkeypatch.setattr(api, "send_manual_payment_link_request", send)
    path = f"/reservations/sama/{data['request_id']}/confirm/"
    first = post(path, guest(quote))
    assert first.status_code == 200, first.json()
    assert first.json()["booking"]["hostaway_created"] is True
    assert first.json()["booking"]["accounting_result"] == "sent"
    assert post(path, guest(quote)).status_code == 200
    assert len(creates) == len(sends) == Reservation.objects.count() == 1
    assert Reservation.objects.get().guests == 3
    assert Reservation.objects.get().payment_status == "unpaid"
    assert "guest@example.invalid" not in str(first.json())


@ENABLED
@pytest.mark.parametrize(
    "change",
    [
        {"accepted_total": "0.01"},
        {"currency": "MAD"},
        {"terms_accepted": False},
        {"guest_email": "not-valid"},
    ],
)
def test_confirmation_requires_actual_quote_consent_and_guest_details(monkeypatch, change):
    data, quote, _ = prepared(monkeypatch)
    path = f"/reservations/sama/{data['request_id']}/confirm/"
    result = post(path, {**guest(quote), **change})
    assert result.status_code in (400, 409)
    assert Reservation.objects.count() == 0
    assert ManualBookingDraft.objects.get().sama_confirmation_started_at is None


@ENABLED
def test_ambiguous_provider_result_blocks_all_replays(monkeypatch):
    data, quote, _ = prepared(monkeypatch)
    calls = []

    def uncertain(**kwargs):
        calls.append(1)
        raise TimeoutError("Sensitive provider detail must never escape")

    monkeypatch.setattr(api, "create_manual_booking_in_hostaway", uncertain)
    path = f"/reservations/sama/{data['request_id']}/confirm/"
    first = post(path, guest(quote))
    assert first.json()["code"] == "review_required"
    assert post(path, guest(quote)).json()["code"] == "review_required"
    assert len(calls) == 1 and "Sensitive" not in str(first.json())


def test_owner_form_checks_apartment_capacity_and_preserves_guest_count():
    property_obj = make_property()
    available = make_availability(property_obj)
    values = {
        "property": property_obj.pk,
        "check_in": available.quote.check_in,
        "check_out": available.quote.check_out,
        "guests": 3,
    }
    form = ManualBookingAvailabilityForm(values)
    assert form.is_valid() and form.cleaned_data["guests"] == 3
    assert not ManualBookingAvailabilityForm({**values, "guests": 5}).is_valid()


@ENABLED
def test_accounting_failure_does_not_duplicate_or_erase_actual_booking(monkeypatch):
    data, quote, _ = prepared(monkeypatch)
    sends = []
    monkeypatch.setattr(
        api,
        "create_manual_booking_in_hostaway",
        lambda **kwargs: create_manual_booking_in_hostaway(
            **kwargs, booking_service=FakeManualHostawayBookingService()
        ),
    )

    def failed(**kwargs):
        sends.append(1)
        raise TimeoutError("Uncertain accounting send")

    monkeypatch.setattr(api, "send_manual_payment_link_request", failed)
    path = f"/reservations/sama/{data['request_id']}/confirm/"
    first = post(path, guest(quote)).json()
    assert first["booking"]["hostaway_created"] is True
    assert first["booking"]["reservation_reference"] == Reservation.objects.get().public_reference
    assert first["booking"]["accounting_result"] == "review_required"
    assert post(path, guest(quote)).json()["code"] == "booked_awaiting_payment"
    assert len(sends) == Reservation.objects.count() == 1


@ENABLED
def test_status_excludes_owner_drafts_not_created_by_sama(monkeypatch):
    data, _, _ = prepared(monkeypatch)
    owner = get_user_model().objects.create_superuser(
        username="owner", email="owner@example.invalid", password="synthetic-only"
    )
    draft = ManualBookingDraft.objects.get()
    draft.created_by = owner
    draft.save(update_fields=["created_by"])
    result = Client().get(f"/reservations/sama/{data['request_id']}/", secure=True, **HEADERS)
    assert result.status_code == 404
