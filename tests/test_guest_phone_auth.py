"""Fictional identities only. Provider, queue and clock are isolated from production."""

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from threading import Barrier
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from django.contrib.auth.models import AnonymousUser
from django.db import connection, connections
from django.test import Client
from django.utils import timezone

from apps.accounts import phone_auth as auth
from apps.accounts.models import (
    OperationsManager,
    PhoneAuthLimit,
    PhoneChallenge,
    VerifiedPhone,
    profile_for,
)
from apps.accounts.phone_copy import TEXT
from apps.accounts.phone_views import PhoneForm
from apps.loyalty.backup import _models, _schema_hash
from apps.loyalty.models import Ledger, Member
from tests.test_loyalty_stage8 import booking, user

pytestmark = pytest.mark.django_db
PHONE = "+966501234567"
CODE = "123456"


@pytest.mark.parametrize(
    ("number", "country", "canonical", "region"),
    [
        ("0012125551234", "SA", "+12125551234", "US"),
        ("+1 (416) 555-1234", "SA", "+14165551234", "CA"),
        ("٠٠١٢١٢٥٥٥١٢٣٤", "SA", "+12125551234", "US"),
        ("2125551234", "US", "+12125551234", "US"),
        ("0501234567", "SA", "+966501234567", "SA"),
        ("0033612345678", "SA", "+33612345678", "FR"),
        ("02 36618 300", "IT", "+390236618300", "IT"),
    ],
)
def test_phone_format_only_normalizes_without_sending_or_creating_accounts(
    number, country, canonical, region, isolated
):
    response = Client().post("/account/mobile/format/", {"phone": number, "country": country})
    assert response.status_code == 200
    assert response.json() == {"phone": canonical, "country": region}
    assert "no-store" in response["Cache-Control"]
    assert not PhoneChallenge.objects.exists() and not VerifiedPhone.objects.exists()
    assert not isolated.dispatched and not isolated.sent
    assert not Member.objects.exists() and not Ledger.objects.exists()


def test_phone_format_does_not_disclose_existing_binding(guest, isolated):
    client = Client()
    before = client.post("/account/mobile/format/", {"phone": PHONE, "country": "SA"})
    VerifiedPhone.objects.create(user=guest.user, phone=PHONE)
    after = client.post("/account/mobile/format/", {"phone": PHONE, "country": "SA"})
    assert before.json() == after.json() == {"phone": PHONE, "country": "SA"}
    assert not PhoneChallenge.objects.exists() and not isolated.sent


def test_phone_format_csrf_gate_rate_limits_and_errors(settings, isolated, monkeypatch):
    url = "/account/mobile/format/"
    body = {"phone": PHONE, "country": "SA"}
    assert Client(enforce_csrf_checks=True).post(url, body).status_code == 403
    client = Client()
    assert client.get(url).status_code == 405
    for phone, country in [("12345", "SA"), (PHONE, "XX"), ("9" * 33, "SA")]:
        response = client.post(url, {"phone": phone, "country": country})
        assert response.status_code == 400
        assert response.json() == {"detail": "invalid_phone"}
    monkeypatch.setattr("apps.accounts.phone_views._limit", lambda *args: False)
    assert client.post(url, body).status_code == 429
    settings.ACCOUNT_WHATSAPP_AUTH_ENABLED = False
    assert client.post(url, body).status_code == 404
    assert not PhoneChallenge.objects.exists() and not isolated.sent


@pytest.mark.parametrize("language", ["ar", "en", "fr"])
def test_phone_pages_share_checkout_widget_and_keep_primary_methods(language, guest, settings):
    for client, url in [
        (Client(), "/account/mobile/"),
        (guest.client, "/account/mobile/link/"),
    ]:
        client.cookies[settings.LANGUAGE_COOKIE_NAME] = language
        response = client.get(url)
        html = response.content.decode()
        assert response.status_code == 200
        for marker in [
            "data-account-phone",
            "checkout-phone.js",
            "checkout-phone.css",
            "/account/mobile/format/",
            "account-phone-validation",
            TEXT[language]["phone_help"],
        ]:
            assert marker in html


def test_phone_final_post_normalizes_double_zero_without_javascript(monkeypatch):
    received = []
    monkeypatch.setattr(
        "apps.accounts.phone_views.begin",
        lambda request, phone, **kwargs: received.append(phone),
    )
    response = Client().post("/account/mobile/", {"phone": "0012125551234", "phone_country": "SA"})
    assert response.status_code == 200
    assert received == ["+12125551234"]


@pytest.fixture(autouse=True)
def isolated(settings, monkeypatch):
    settings.ACCOUNT_WHATSAPP_AUTH_ENABLED = True
    settings.ACCOUNT_WHATSAPP_AUTH_ISOLATED = True
    settings.ACCOUNT_WHATSAPP_AUTH_EMAILS = ["loyalty-phone@example.invalid"]
    settings.ULTRAMSG_ENABLED = True
    settings.ULTRAMSG_INSTANCE_ID = "test-only"
    settings.ULTRAMSG_TOKEN = "test-only"
    clock = [datetime(2026, 10, 16, 1, 30, tzinfo=UTC)]
    monkeypatch.setattr(timezone, "now", lambda: clock[0])
    monkeypatch.setattr(auth.secrets, "randbelow", lambda n: 123456)
    dispatched, sent = [], []
    monkeypatch.setattr(auth, "_dispatch", lambda key: dispatched.append(key))

    class Provider:
        def __init__(self):
            self.http = SimpleNamespace(
                get=Mock(
                    return_value=SimpleNamespace(
                        status_code=200, json=lambda: {"status": {"accountStatus": "authenticated"}}
                    )
                )
            )

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def send_text(self, **kwargs):
            sent.append(kwargs)

    monkeypatch.setattr(auth, "UltraMsgClient", Provider)
    return SimpleNamespace(clock=clock, dispatched=dispatched, sent=sent)


@pytest.fixture
def guest():
    u = user("phone")
    r = booking(u, paid=False, arrived=False)
    r.booking_intent.guest_phone = PHONE
    r.booking_intent.save(update_fields=["guest_phone"])
    client = Client()
    client.force_login(u)
    return SimpleNamespace(user=u, booking=r, client=client)


def start(client, callbacks, *, binding=False, phone=PHONE, target="/my-bookings/rewards/"):
    with callbacks(execute=True):
        response = client.post(
            "/account/mobile/link/" if binding else "/account/mobile/",
            {"phone": phone, "next": target},
        )
    return response


def deliver(client):
    key = client.session[auth.PENDING]
    auth.deliver_phone_code(key)
    return PhoneChallenge.objects.get(pk=key)


def test_binding_requires_existing_primary_auth_then_one_use_code(
    guest, isolated, django_capture_on_commit_callbacks
):
    response = start(guest.client, django_capture_on_commit_callbacks, binding=True)
    assert response.status_code == 302
    row = deliver(guest.client)
    assert row.code_digest == auth._digest("code", f"{row.pk}:{CODE}")
    assert isolated.dispatched == [str(row.pk)]
    assert auth.MARKER in isolated.sent[0]["body"]
    assert isolated.sent[0]["recipient"] == PHONE
    assert not VerifiedPhone.objects.exists()
    assert (
        guest.client.post("/account/mobile/confirm/", {"code": CODE}).url == "/my-bookings/details/"
    )
    assert VerifiedPhone.objects.get().user_id == guest.user.pk
    row.refresh_from_db()
    assert row.status == "consumed" and not row.code_digest
    guest.booking.refresh_from_db()
    assert not guest.booking.loyalty_state.get("arrival")
    assert not Member.objects.exists() and not Ledger.objects.exists()


def test_bound_guest_signs_in_without_password_and_cannot_rebind_with_phone_session(
    guest, django_capture_on_commit_callbacks
):
    VerifiedPhone.objects.create(user=guest.user, phone=PHONE)
    client = Client()
    response = start(client, django_capture_on_commit_callbacks)
    assert response.url == "/account/mobile/confirm/"
    row = deliver(client)
    response = client.post("/account/mobile/confirm/", {"code": "١٢٣٤٥٦"})
    assert response.url == "/my-bookings/rewards/"
    assert client.session["_auth_user_id"] == str(guest.user.pk)
    assert auth.PRIMARY_AUTH not in client.session
    assert client.post("/account/mobile/confirm/", {"code": CODE}).status_code == 302
    row.refresh_from_db()
    assert row.attempts == 1


@pytest.mark.parametrize(
    "reason",
    [
        "stale",
        "no-proof",
        "wrong-phone",
        "contact-only",
        "staff",
        "super",
        "owner",
        "unverified",
        "scope",
    ],
)
def test_binding_denies_untrusted_identity(
    guest, settings, reason, django_capture_on_commit_callbacks
):
    if reason in {"stale", "no-proof"}:
        session = guest.client.session
        session.pop(auth.PRIMARY_AUTH, None)
        if reason == "stale":
            session[auth.PRIMARY_AUTH] = {
                "user": guest.user.pk,
                "at": int(timezone.now().timestamp()) - 901,
            }
        session.save()
    elif reason == "wrong-phone":
        guest.booking.booking_intent.guest_phone = "+966501234568"
        guest.booking.booking_intent.save(update_fields=["guest_phone"])
    elif reason == "contact-only":
        guest.booking.booking_intent.customer = None
        guest.booking.booking_intent.save(update_fields=["customer"])
        profile_for(guest.user).phone = PHONE
        profile_for(guest.user).save()
    elif reason in {"staff", "super"}:
        setattr(guest.user, "is_staff" if reason == "staff" else "is_superuser", True)
        guest.user.save()
    elif reason == "owner":
        settings.OPERATIONS_OWNER_EMAIL = guest.user.email
    elif reason == "unverified":
        profile = profile_for(guest.user)
        profile.email_verified_at = None
        profile.save()
    elif reason == "scope":
        settings.ACCOUNT_WHATSAPP_AUTH_EMAILS = []
    response = start(guest.client, django_capture_on_commit_callbacks, binding=True)
    assert response.status_code in {200, 404}
    assert not PhoneChallenge.objects.exists()
    assert not VerifiedPhone.objects.exists()


@pytest.mark.parametrize(
    "field", ["ACCOUNT_WHATSAPP_AUTH_ENABLED", "ACCOUNT_WHATSAPP_AUTH_ISOLATED"]
)
def test_closed_gates_preserve_original_login(guest, settings, field):
    setattr(settings, field, False)
    c = Client()
    assert c.get("/account/mobile/").status_code == 404
    page = c.get("/login/")
    assert page.status_code == 200
    assert b'name="email"' in page.content
    assert b"/account/mobile/" not in page.content


def test_unknown_number_never_looks_up_an_unverified_contact(
    guest, django_capture_on_commit_callbacks, isolated
):
    p = profile_for(guest.user)
    p.phone = PHONE
    p.save()
    client = Client()
    assert start(client, django_capture_on_commit_callbacks).status_code == 302
    row = deliver(client)
    assert row.user_id is None
    assert not isolated.sent
    assert client.post("/account/mobile/confirm/", {"code": CODE}).status_code in {200, 302}
    assert "_auth_user_id" not in client.session


@pytest.mark.parametrize(
    "reason", ["expired", "role-changed", "binding-changed", "session-changed", "five-wrong"]
)
def test_login_challenge_rejects_replay_or_changed_evidence(
    guest, isolated, reason, django_capture_on_commit_callbacks
):
    binding = VerifiedPhone.objects.create(user=guest.user, phone=PHONE)
    client = Client()
    start(client, django_capture_on_commit_callbacks)
    row = deliver(client)
    if reason == "expired":
        isolated.clock[0] += timedelta(seconds=301)
    elif reason == "role-changed":
        guest.user.is_staff = True
        guest.user.save()
    elif reason == "binding-changed":
        binding.delete()
    elif reason == "session-changed":
        session = client.session
        session["booking_session_marker"] = "different-session"
        session.save()
    elif reason == "five-wrong":
        for _ in range(5):
            client.post("/account/mobile/confirm/", {"code": "654321"})
    assert client.post("/account/mobile/confirm/", {"code": CODE}).status_code in {200, 302}
    assert "_auth_user_id" not in client.session
    row.refresh_from_db()
    assert row.status != "consumed"


def test_resend_supersedes_code_without_resetting_phone_limits(
    guest, isolated, django_capture_on_commit_callbacks
):
    VerifiedPhone.objects.create(user=guest.user, phone=PHONE)
    client = Client()
    start(client, django_capture_on_commit_callbacks)
    first = deliver(client)
    assert start(Client(), django_capture_on_commit_callbacks).status_code == 200
    isolated.clock[0] += timedelta(seconds=61)
    start(client, django_capture_on_commit_callbacks)
    first.refresh_from_db()
    assert first.status == "superseded" and not first.code_digest
    assert PhoneAuthLimit.objects.filter(count__gt=1).exists()


@pytest.mark.parametrize("failure", ["disconnected", "timeout", "late"])
def test_provider_failure_never_authenticates_or_logs_code(
    guest, isolated, monkeypatch, failure, caplog, django_capture_on_commit_callbacks
):
    VerifiedPhone.objects.create(user=guest.user, phone=PHONE)
    c = Client()
    start(c, django_capture_on_commit_callbacks)
    if failure == "late":
        isolated.clock[0] += timedelta(seconds=301)
    else:

        class Broken:
            def __enter__(self):
                if failure == "timeout":
                    raise RuntimeError(f"secret-provider-request-{CODE}")
                self.http = SimpleNamespace(
                    get=lambda *a, **k: SimpleNamespace(
                        status_code=200, json=lambda: {"status": {"accountStatus": "disconnected"}}
                    )
                )
                return self

            def __exit__(self, *args):
                pass

        monkeypatch.setattr(auth, "UltraMsgClient", Broken)
    row = deliver(c)
    assert row.status != "sent"
    assert CODE not in caplog.text and not isolated.sent
    c.post("/account/mobile/confirm/", {"code": CODE})
    assert "_auth_user_id" not in c.session


@pytest.mark.parametrize("language", ["ar", "en", "fr"])
def test_three_language_additional_option_and_safe_next(
    guest, settings, language, django_capture_on_commit_callbacks
):
    settings.GOOGLE_SIGN_IN_ENABLED = settings.APPLE_SIGN_IN_ENABLED = True
    settings.SOCIALACCOUNT_PROVIDERS = {
        "google": {"APP": {"client_id": "test-only", "secret": "test-only"}},
        "apple": {"APP": {"client_id": "test-only", "secret": "test-only", "key": "test"}},
    }
    c = Client()
    c.cookies[settings.LANGUAGE_COOKIE_NAME] = language
    page = c.get("/login/?next=/my-bookings/rewards/%3Finvite%3Dtest")
    assert TEXT[language]["option"].encode() in page.content
    assert b'name="email"' in page.content
    assert b"auth-social" in page.content
    assert b'action="/accounts/google/login/?' in page.content
    assert b'action="/accounts/apple/login/?' in page.content
    VerifiedPhone.objects.create(user=guest.user, phone=PHONE)
    start(c, django_capture_on_commit_callbacks, target="https://evil.example.invalid/")
    deliver(c)
    assert c.post("/account/mobile/confirm/", {"code": CODE}).url == "/my-bookings/"


def test_credentials_not_added_to_loyalty_backup_schema(guest):
    assert "accounts.verifiedphone" not in _models()
    assert "accounts.phonechallenge" not in _models()
    before = _schema_hash()
    VerifiedPhone.objects.create(user=guest.user, phone=PHONE)
    assert _schema_hash() == before
    assert not Member.objects.exists() and not Ledger.objects.exists()


def test_operations_manager_without_staff_is_not_a_phone_login(
    guest, django_capture_on_commit_callbacks
):
    OperationsManager.objects.create(email=guest.user.email)
    assert start(guest.client, django_capture_on_commit_callbacks, binding=True).status_code == 404
    assert not PhoneChallenge.objects.exists()


def test_provider_gate_and_no_csrf_cannot_send(guest, settings, django_capture_on_commit_callbacks):
    assert (
        Client(enforce_csrf_checks=True).post("/account/mobile/", {"phone": PHONE}).status_code
        == 403
    )
    settings.ULTRAMSG_ENABLED = False
    assert start(guest.client, django_capture_on_commit_callbacks, binding=True).status_code == 404
    assert not PhoneChallenge.objects.exists()


def test_minute_boundary_does_not_reset_cooldown(
    guest, isolated, django_capture_on_commit_callbacks
):
    VerifiedPhone.objects.create(user=guest.user, phone=PHONE)
    isolated.clock[0] = isolated.clock[0].replace(second=59)
    start(Client(), django_capture_on_commit_callbacks)
    isolated.clock[0] += timedelta(seconds=2)
    assert start(Client(), django_capture_on_commit_callbacks).status_code == 200
    assert PhoneChallenge.objects.count() == 1


def test_phone_number_bound_elsewhere_is_not_reassigned(guest, django_capture_on_commit_callbacks):
    other = user("other-phone")
    identity = VerifiedPhone.objects.create(user=other, phone=PHONE)
    assert start(guest.client, django_capture_on_commit_callbacks, binding=True).status_code == 200
    identity.refresh_from_db()
    assert identity.user_id == other.pk
    assert not PhoneChallenge.objects.exists()


def test_contact_edit_does_not_modify_the_login_credential(guest):
    identity = VerifiedPhone.objects.create(user=guest.user, phone=PHONE)
    response = guest.client.post("/my-bookings/details/", {"phone": "+966501234568"})
    assert response.status_code == 302
    identity.refresh_from_db()
    assert identity.phone == PHONE
    assert guest.client.get("/my-bookings/details/").context["phone_binding"].pk == identity.pk


@pytest.mark.parametrize(
    "number,country,expected",
    [
        ("٠٥٠١٢٣٤٥٦٧", "SA", PHONE),
        ("0612345678", "FR", "+33612345678"),
        ("+966 50 123 4567", "FR", PHONE),
    ],
)
def test_explicit_country_normalizes_local_and_international_numbers(number, country, expected):
    form = PhoneForm({"phone": number, "phone_country": country})
    assert form.is_valid(), form.errors
    assert form.cleaned_data["phone"] == expected


@pytest.mark.django_db(transaction=True)
def test_postgres_one_code_cannot_authenticate_twice(guest, django_capture_on_commit_callbacks):
    if connection.vendor != "postgresql":
        pytest.skip("PostgreSQL row-lock concurrency requires the isolated database job")
    VerifiedPhone.objects.create(user=guest.user, phone=PHONE)
    client = Client()
    start(client, django_capture_on_commit_callbacks)
    deliver(client)
    snapshot = dict(client.session)
    barrier = Barrier(2)

    def consume_once():
        connections.close_all()
        try:
            request = SimpleNamespace(
                session=dict(snapshot), META={"REMOTE_ADDR": "127.0.0.1"}, user=AnonymousUser()
            )
            barrier.wait(timeout=5)
            return auth.consume(request, CODE) is not None
        finally:
            connections.close_all()

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: consume_once(), range(2)))
    assert sorted(results) == [False, True]
    row = PhoneChallenge.objects.get()
    assert row.status == "consumed" and row.attempts == 1
    # A numeric OTP can coincidentally be a substring of the phone/UUID.
    # Audit the credential fields, rather than rejecting unrelated identifiers.
    assert not {"code", "body", "message"} & {f.name for f in row._meta.concrete_fields}
    assert row.code_digest == ""
