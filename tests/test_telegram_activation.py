"""Synthetic activation; all cloud calls replaced, no provider or guest IO."""

import os
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path
from threading import Event
from types import SimpleNamespace

import httpx
import pytest
from django.contrib.auth import get_user_model
from django.db import close_old_connections, connection
from django.test import Client, RequestFactory
from django.utils import timezone, translation

from apps.accounts.models import VerifiedPhone
from apps.accounts.phone_auth import PRIMARY_AUTH
from apps.notifications.models import AuditLog
from apps.reservations import telegram_activation as service
from apps.reservations import telegram_backend as backend
from apps.reservations.models import Reservation, TelegramActivation
from tests.test_account_booking_claim import make_reservation

pytestmark = pytest.mark.django_db
URL = "https://t.me/LuxurySmartApartmentsBot?start=b_" + "a" * 48


@pytest.fixture
def rig(settings, monkeypatch):
    settings.TELEGRAM_ACTIVATION_ENABLED = True
    settings.TELEGRAM_ACTIVATION_SERVICE_KEY = "synthetic-server-secret"
    booking = make_reservation()
    Reservation.objects.filter(pk=booking.pk).update(
        hostaway_reservation_id=70012345, normalized_status="confirmed", is_test=False
    )
    booking.refresh_from_db()
    user = get_user_model().objects.create_user("tg-guest", email="tg@example.invalid")
    booking.booking_intent.customer = user
    booking.booking_intent.save(update_fields=["customer"])
    VerifiedPhone.objects.create(user=user, phone=booking.booking_intent.guest_phone)
    state = SimpleNamespace(calls=[], outcome="issued", status="unverified", fail=False)

    class FakeBackend:
        def status(self, key):
            return state.status

        def rpc(self, name, data):
            state.calls.append((name, data))
            if state.fail:
                raise backend.Unavailable
            if name.endswith("approve_phone"):
                return {"action": "approved"}
            return {
                "action": state.outcome,
                "url": URL,
                "expires_at": (timezone.now() + timedelta(hours=2)).isoformat(),
            }

        def close(self):
            pass

    monkeypatch.setattr(backend, "Backend", FakeBackend)
    request = RequestFactory().post("/private-activation/", {})
    request.user = user
    request.session = {PRIMARY_AUTH: {"user": user.pk, "at": int(timezone.now().timestamp())}}
    return SimpleNamespace(booking=booking, user=user, request=request, state=state)


def test_default_off_no_network_or_attempt(rig, settings):
    settings.TELEGRAM_ACTIVATION_ENABLED = False
    assert service.issue(rig.request, rig.booking)["state"] == "disabled"
    assert service.context(rig.request, rig.booking)["state"] == "disabled"
    assert not TelegramActivation.objects.exists() and not rig.state.calls


def test_verified_issue_once_no_capability_storage(rig):
    first = service.issue(rig.request, rig.booking)
    assert first["url"] == URL and first["state"] == "issued"
    assert service.issue(rig.request, rig.booking) == {"state": "active", "url": ""}
    assert len(rig.state.calls) == 2
    row = TelegramActivation.objects.get()
    assert row.proof_kind == "website_verified"
    assert URL not in str(list(TelegramActivation.objects.values()))
    assert rig.booking.booking_intent.guest_phone not in str(row.__dict__)
    assert VerifiedPhone.objects.count() == 1


@pytest.mark.parametrize("kind", ["unverified", "different", "stale_login", "wrong_customer"])
def test_management_access_is_not_phone_proof(rig, kind):
    if kind == "unverified":
        VerifiedPhone.objects.all().delete()
    elif kind == "different":
        VerifiedPhone.objects.update(phone="+12025550123")
    elif kind == "stale_login":
        rig.request.session.clear()
    else:
        rig.booking.booking_intent.customer = None
        rig.booking.booking_intent.save(update_fields=["customer"])
    assert service.issue(rig.request, rig.booking)["state"] == "unverified"
    assert not rig.state.calls and not TelegramActivation.objects.exists()


def test_admin_external_booking_requires_independent_evidence(rig):
    rig.booking.booking_intent = None
    rig.booking.source_type = "external_channel"
    rig.booking.save(update_fields=["booking_intent", "source_type"])
    request = RequestFactory().post("/private-admin/", {"phone": "+12025550123"})
    request.user = rig.user
    assert service.issue(request, rig.booking, admin=True)["state"] == "unverified"
    request = RequestFactory().post(
        "/private-admin/",
        {"phone": "+12025550123", "evidence_ref": "channel-proof:test-001", "confirmed": "on"},
    )
    request.user = rig.user
    assert service.issue(request, rig.booking, admin=True)["state"] == "issued"
    assert rig.state.calls[0][1]["p_phone"] == "12025550123"
    assert rig.state.calls[0][1]["p_evidence_ref"].startswith("admin_verified:")
    assert VerifiedPhone.objects.get(user=rig.user).phone != "+12025550123"


def test_unknown_is_durable_and_never_retried(rig):
    rig.state.fail = True
    assert service.issue(rig.request, rig.booking)["state"] == "uncertain"
    rig.state.fail = False
    assert service.issue(rig.request, rig.booking)["state"] == "uncertain"
    assert len(rig.state.calls) == 1


def test_explicit_expired_replacement_but_no_rebinding(rig):
    service.issue(rig.request, rig.booking)
    TelegramActivation.objects.update(expires_at=timezone.now() - timedelta(seconds=1))
    assert service.issue(rig.request, rig.booking)["state"] == "issued"
    TelegramActivation.objects.update(state="bound")
    assert service.issue(rig.request, rig.booking)["state"] == "bound"
    assert len(rig.state.calls) == 4
    proofs = AuditLog.objects.filter(action="telegram.activation_phone_proof")
    assert proofs.count() == 2
    assert len({row.metadata["proof_id"] for row in proofs}) == 2


def test_owner_http_issuance_with_csrf_and_external_booking(rig, settings):
    owner = get_user_model().objects.create_superuser(
        username=settings.OPERATIONS_OWNER_EMAIL,
        email=settings.OPERATIONS_OWNER_EMAIL,
        password="synthetic-test-only",
    )
    rig.booking.booking_intent = None
    rig.booking.source_type = "external_channel"
    rig.booking.save(update_fields=["booking_intent", "source_type"])
    client = Client(enforce_csrf_checks=True)
    client.force_login(owner)
    path = f"/admin/operations/bookings/{rig.booking.pk}/"
    data = {
        "action": "telegram_activation",
        "phone": "+12025550123",
        "evidence_ref": "channel:test-proof",
        "confirmed": "on",
    }
    assert client.post(path, data).status_code == 403
    client.cookies["csrftoken"] = "a" * 32
    response = client.post(path, data, HTTP_X_CSRFTOKEN="a" * 32)
    assert response.status_code == 200 and URL.encode() in response.content
    assert response["Referrer-Policy"] == "no-referrer"
    assert client.post(path, data, HTTP_X_CSRFTOKEN="a" * 32).status_code == 200
    assert len(rig.state.calls) == 2


def test_cancelled_or_missing_provider_id_never_issues(rig):
    rig.booking.normalized_status = "cancelled"
    assert service.issue(rig.request, rig.booking)["state"] == "closed"
    rig.booking.normalized_status = "confirmed"
    rig.booking.hostaway_reservation_id = None
    assert service.issue(rig.request, rig.booking)["state"] == "closed"
    assert not rig.state.calls


@pytest.mark.parametrize("state", ["bound", "active", "expired", "unverified"])
def test_private_ui_states(rig, state):
    rig.state.status = state
    result = service.context(rig.request, rig.booking)
    assert result["state"] == state
    assert result["can_issue"] == (state in {"expired", "unverified"})
    assert URL not in str(result)


def test_guest_endpoint_scope_method_csrf_and_privacy(rig):
    path = f"/reservations/manage/{rig.booking.public_reference}/telegram/"
    client = Client()
    assert client.post(path).status_code == 404
    client.force_login(rig.user)
    assert client.get(path).status_code == 405
    assert Client(enforce_csrf_checks=True).post(path).status_code == 403
    response = client.post(path)
    assert response.status_code == 200 and URL.encode() in response.content
    assert "no-store" in response["Cache-Control"]
    assert response["Referrer-Policy"] == "no-referrer"
    assert "script-src 'none'" in response["Content-Security-Policy"]
    assert b"noreferrer noopener nofollow" in response.content
    assert URL not in response.get("Location", "")


def test_admin_endpoint_rejects_guest(rig):
    client = Client()
    client.force_login(rig.user)
    response = client.post(
        f"/admin/operations/bookings/{rig.booking.pk}/",
        {
            "action": "telegram_activation",
            "phone": "+12025550123",
            "evidence_ref": "channel:test-proof",
            "confirmed": "on",
        },
    )
    assert response.status_code == 403 and not rig.state.calls


@pytest.mark.parametrize(
    "url", ["https://evil.invalid/", URL + "&phone=bad", URL.replace("Bot", "OtherBot")]
)
def test_no_destination_override(url):
    with pytest.raises(backend.Unavailable):
        backend.checked_invitation(
            {"url": url, "expires_at": (timezone.now() + timedelta(hours=2)).isoformat()}
        )


@pytest.mark.parametrize(
    "change", ["not_ready", "missing_component", "wrong_contract", "malformed"]
)
def test_http_200_is_not_readiness(settings, monkeypatch, change):
    settings.TELEGRAM_ACTIVATION_ENABLED = True
    settings.TELEGRAM_ACTIVATION_SERVICE_KEY = "test-only"
    value = {
        "contract": "tg20_guest_journey_v1",
        "ready": True,
        "dispatch_enabled": False,
        "cutover_configured": False,
        "components": dict.fromkeys(("s01", "reminders", "reviews", "contacts", "loyalty"), True),
    }
    if change == "not_ready":
        value["ready"] = False
    elif change == "missing_component":
        value["components"]["loyalty"] = False
    elif change == "wrong_contract":
        value["contract"] = "old"
    else:
        value = []
    calls = []
    original = httpx.Client

    def handle(request):
        calls.append(request.url.path)
        return httpx.Response(200, json=value)

    monkeypatch.setattr(
        backend.httpx, "Client", lambda **kw: original(**kw, transport=httpx.MockTransport(handle))
    )
    client = backend.Backend()
    try:
        with pytest.raises(backend.NotReady):
            client.rpc("tg20_s01_issue_invitation", {"p_key": "70012345"})
    finally:
        client.close()
    assert calls == ["/rest/v1/rpc/tg20_s01_health"]


def test_fixed_backend_ready_approve_issue_and_private_binding(settings, monkeypatch):
    from hashlib import sha256

    settings.TELEGRAM_ACTIVATION_ENABLED = True
    settings.TELEGRAM_ACTIVATION_SERVICE_KEY = "test-only"
    original, calls = httpx.Client, []

    def handle(request):
        calls.append(request.url.path)
        assert request.url.host == "rsqgwepnowpnpealrzbn.supabase.co"
        if request.url.path.endswith("tg20_s01_health"):
            return httpx.Response(
                200,
                json={
                    "contract": "tg20_guest_journey_v1",
                    "ready": True,
                    "dispatch_enabled": False,
                    "cutover_configured": False,
                    "components": dict.fromkeys(
                        ("s01", "reminders", "reviews", "contacts", "loyalty"), True
                    ),
                },
            )
        if request.url.path.endswith("tg20_s01_bindings"):
            return httpx.Response(
                200,
                json=[
                    {
                        "user_id": "12345678",
                        "chat_id": "12345678",
                        "revoked": False,
                        "phone_hash": sha256(b"12025550123").hexdigest(),
                    }
                ],
            )
        return httpx.Response(200, json={"action": "approved"})

    monkeypatch.setattr(
        backend.httpx, "Client", lambda **kw: original(**kw, transport=httpx.MockTransport(handle))
    )
    client = backend.Backend()
    try:
        assert client.binding("70012345", "+12025550123") == "12345678"
        with pytest.raises(backend.Unavailable):
            client.binding("70012345", "+12025550999")
        assert client.rpc("tg20_s01_approve_phone", {})["action"] == "approved"
    finally:
        client.close()
    assert sum(path.endswith("tg20_s01_health") for path in calls) == 1


def test_render_telegram_review_layout(rig, tmp_path):
    from tests.test_contact_layout import _standalone

    output = Path(os.environ.get("LSA_CONTACT_VISUAL_DIR", tmp_path))
    output.mkdir(parents=True, exist_ok=True)
    for language, state in (
        ("ar", "issued"),
        ("en", "issued"),
        ("fr", "issued"),
        ("ar", "bound"),
        ("ar", "expired"),
        ("ar", "unverified"),
    ):
        with translation.override(language):
            response = service.result_response(
                rig.request,
                {
                    "state": state,
                    "url": URL if state == "issued" else "",
                    "expires_at": timezone.now() + timedelta(hours=2),
                },
            )
            html = response.content.decode()
            assert "telegram-activation-card" in html
            assert ("noreferrer noopener nofollow" in html) == (state == "issued")
            (output / f"telegram-{language}-{state}.html").write_text(_standalone(html))
    from django.template import RequestContext, Template

    rig.request.path = "/admin/operations/bookings/synthetic/"
    card = service.context(rig.request, rig.booking, admin=True)
    template = Template(
        '{% extends "base.html" %}{% block content %}'
        '{% include "reservations/_telegram_activation.html" %}{% endblock %}'
    )
    html = template.render(RequestContext(rig.request, {"telegram": card}))
    (output / "telegram-ar-admin.html").write_text(_standalone(html))


@pytest.mark.django_db(transaction=True)
def test_concurrent_issuance_one_rpc_sequence(rig, monkeypatch):
    if connection.vendor != "postgresql":
        pytest.skip("Requires PostgreSQL row locks")
    entered, release = Event(), Event()
    original = backend.Backend.rpc

    def slow(self, name, data):
        if name.endswith("approve_phone"):
            entered.set()
            assert release.wait(10)
        return original(self, name, data)

    monkeypatch.setattr(backend.Backend, "rpc", slow)

    def run():
        close_old_connections()
        try:
            return service.issue(rig.request, rig.booking)["state"]
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(run)
        try:
            assert entered.wait(10)
            assert pool.submit(run).result(timeout=10) == "processing"
        finally:
            release.set()
        assert first.result(timeout=10) == "issued"
    assert len(rig.state.calls) == 2
