"""Fictional identities only. Provider, queue and clock are isolated from production."""

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from threading import Barrier
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from django.contrib.auth import get_user_model
from django.contrib.auth.models import AnonymousUser
from django.db import connection, connections
from django.test import Client
from django.utils import timezone, translation

from apps.accounts import phone_auth as auth
from apps.accounts.models import (
    OperationsManager,
    PhoneAuthLimit,
    PhoneChallenge,
    VerifiedPhone,
    profile_for,
)
from apps.accounts.phone_copy import TEXT
from apps.accounts.phone_messages import AUTH_MARKER, AUTH_TITLES, phone_message
from apps.accounts.phone_views import PhoneForm
from apps.accounts.tokens import make_verification_code
from apps.loyalty.backup import _models, _schema_hash
from apps.loyalty.models import Ledger, Member
from apps.reservations.models import Reservation
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


@pytest.mark.parametrize(
    ("phone", "country", "expected"),
    [
        ("0012125551234", "SA", "+12125551234"),
        ("٠٠١٢١٢٥٥٥١٢٣٤", "SA", "+12125551234"),
        ("2125551234", "US", "+12125551234"),
        ("0501234567", "SA", "+966501234567"),
        ("", "US", ""),
    ],
)
def test_profile_phone_save_normalizes_without_changing_signin_binding(
    guest, isolated, phone, country, expected, settings
):
    settings.ACCOUNT_WHATSAPP_AUTH_ENABLED = False
    binding = VerifiedPhone.objects.create(user=guest.user, phone=PHONE)
    original_email = guest.user.email
    response = guest.client.post(
        "/my-bookings/details/", {"phone": phone, "phone_country": country}
    )
    assert response.status_code == 302
    assert profile_for(guest.user).phone == expected
    binding.refresh_from_db()
    assert binding.phone == PHONE
    guest.user.refresh_from_db()
    assert guest.user.email == original_email
    assert not PhoneChallenge.objects.exists() and not isolated.dispatched and not isolated.sent


def test_profile_phone_country_comes_from_saved_phone_not_residence(guest):
    profile = profile_for(guest.user)
    profile.phone = "+14165551234"
    profile.residence_country = "Saudi Arabia"
    profile.save(update_fields=["phone", "residence_country"])
    response = guest.client.get("/my-bookings/details/")
    assert response.context["profile_form"]["phone_country"].value() == "CA"
    assert response.context["profile_form"]["phone"].value() == "+14165551234"


def test_profile_format_is_login_protected_and_independent_of_phone_auth(guest, settings, isolated):
    url = "/my-bookings/details/phone-format/"
    settings.ACCOUNT_WHATSAPP_AUTH_ENABLED = False
    assert Client().post(url, {"phone": PHONE}).status_code == 302
    strict = Client(enforce_csrf_checks=True)
    strict.force_login(guest.user)
    assert strict.post(url, {"phone": PHONE}).status_code == 403
    before = profile_for(guest.user).phone
    response = guest.client.post(url, {"phone": "0012125551234", "country": "SA"})
    assert response.status_code == 200
    assert response.json() == {"phone": "+12125551234", "country": "US"}
    assert "no-store" in response["Cache-Control"]
    assert profile_for(guest.user).phone == before
    assert not VerifiedPhone.objects.exists() and not PhoneChallenge.objects.exists()
    assert not isolated.dispatched and not isolated.sent


@pytest.mark.parametrize("language", ["ar", "en", "fr"])
def test_profile_phone_widget_is_localized_and_only_loaded_in_details(guest, language, settings):
    guest.client.cookies[settings.LANGUAGE_COOKIE_NAME] = language
    content = guest.client.get("/my-bookings/details/").content.decode()
    for marker in [
        "data-account-phone",
        'data-draft-server-bound="true"',
        "checkout-phone.js",
        "checkout-phone.css",
        "/my-bookings/details/phone-format/",
        TEXT[language]["phone_help"],
        TEXT[language]["contact_hint"],
    ]:
        assert marker in content
    bookings = guest.client.get("/my-bookings/").content.decode()
    assert "checkout-phone.js" not in bookings
    assert "checkout-phone.css" not in bookings


@pytest.fixture(autouse=True)
def isolated(settings, monkeypatch):
    settings.ACCOUNT_WHATSAPP_AUTH_PUBLIC = False
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
                        status_code=200,
                        json=lambda: {"status": {"accountStatus": {"status": "authenticated"}}},
                    )
                )
            )

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def send_image(self, **kwargs):
            sent.append({**kwargs, "body": kwargs["caption"], "kind": "image"})

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
            {"phone": phone, "phone_country": "SA", "next": target},
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
    assert AUTH_TITLES[row.language] in isolated.sent[0]["body"]
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


@pytest.mark.parametrize("language", ["ar", "en", "fr"])
@pytest.mark.parametrize("purpose", ["bind", "login"])
def test_code_message_uses_payment_identity_and_exactly_one_language(language, purpose):
    from apps.payments.payment_link_tasks import PAYMENT_MESSAGE_BRAND

    body = phone_message(code=CODE, language=language, purpose=purpose)
    assert body.startswith(f"✨ *{PAYMENT_MESSAGE_BRAND}*\n\n")
    assert body.count(CODE) == 1 and f"\n\n*{CODE}*\n\n" in body
    assert AUTH_TITLES[language] in body
    assert body.endswith(f"\n\n{AUTH_MARKER}")
    assert body.count(AUTH_MARKER) == 1
    assert "https://" not in body and "http://" not in body
    for other in {"ar", "en", "fr"} - {language}:
        assert AUTH_TITLES[other] not in body
    assert {
        "ar": "صالح لمدة خمس دقائق ولمرة واحدة.",
        "en": "Valid for five minutes and one use only.",
        "fr": "Valable cinq minutes et une seule fois.",
    }[language] in body
    assert {
        "ar": "لا تشارك هذا الرمز",
        "en": "Never share this code",
        "fr": "ne partagez ce code",
    }[language] in body


@pytest.mark.parametrize(
    ("preferred", "page", "expected"),
    [
        ("fr", "ar", "fr"),
        ("en", "ar", "en"),
        ("ar", "en", "ar"),
        ("", "fr", "fr"),
        ("", "en", "en"),
        ("", "ar", "ar"),
    ],
)
@pytest.mark.parametrize("binding", [True, False])
def test_guest_language_is_saved_before_queue_and_not_taken_from_worker(
    guest,
    isolated,
    preferred,
    page,
    expected,
    binding,
    settings,
    django_capture_on_commit_callbacks,
):
    profile = profile_for(guest.user)
    profile.preferred_language = preferred
    profile.save(update_fields=["preferred_language"])
    if not binding:
        VerifiedPhone.objects.create(user=guest.user, phone=PHONE)
    client = guest.client if binding else Client()
    client.cookies[settings.LANGUAGE_COOKIE_NAME] = page
    assert start(client, django_capture_on_commit_callbacks, binding=binding).status_code == 302
    row = PhoneChallenge.objects.get(pk=client.session[auth.PENDING])
    assert row.language == expected
    # Neither a later account edit nor a worker's ambient locale can change it.
    profile.preferred_language = "ar" if expected != "ar" else "en"
    profile.save(update_fields=["preferred_language"])
    with translation.override("en" if expected != "en" else "fr"):
        row = deliver(client)
    assert row.status == "sent" and AUTH_TITLES[expected] in isolated.sent[0]["body"]
    assert isolated.sent[0]["body"].count(CODE) == 1


@pytest.mark.parametrize("language", ["", "de", "unknown", None, "AR-sa"])
def test_code_message_has_single_arabic_fallback(language):
    body = phone_message(code=CODE, language=language, purpose="bind")
    assert AUTH_TITLES["ar"] in body
    assert AUTH_TITLES["en"] not in body and AUTH_TITLES["fr"] not in body


def test_auth_logo_is_fixed_private_upload_with_one_caption_and_no_duplicate(
    guest, isolated, django_capture_on_commit_callbacks
):
    from apps.accounts.phone_messages import phone_logo

    start(guest.client, django_capture_on_commit_callbacks, binding=True)
    row = deliver(guest.client)
    auth.deliver_phone_code(row.pk)
    assert len(isolated.sent) == 1
    sent = isolated.sent[0]
    assert sent["kind"] == "image" and sent["content"] == phone_logo()
    assert sent["content"].startswith(b"\x89PNG\r\n\x1a\n")
    assert CODE.encode() not in sent["content"]
    assert sent["caption"].count(CODE) == 1 and AUTH_MARKER in sent["caption"]
    assert len(sent["caption"].encode("utf-16-le")) // 2 <= 1024
    assert sent["reference_id"].startswith("lsa-auth-")
    assert "https://" not in sent["caption"]


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


@pytest.mark.parametrize("has_booking", [True, False])
def test_binding_proves_new_mobile_by_code_without_a_matching_booking(
    guest, isolated, django_capture_on_commit_callbacks, has_booking
):
    number = "+12125551234"
    if not has_booking:
        guest.booking.booking_intent.customer = None
        guest.booking.booking_intent.save(update_fields=["customer"])
    before = guest.booking.booking_intent.guest_phone
    profile = profile_for(guest.user)
    profile.phone = number
    profile.save(update_fields=["phone"])
    response = start(
        guest.client, django_capture_on_commit_callbacks, binding=True, phone="0012125551234"
    )
    assert response.status_code == 302
    row = deliver(guest.client)
    assert row.status == "sent" and isolated.sent[0]["recipient"] == number
    assert not VerifiedPhone.objects.exists()  # Saving/dispatching is not proof.
    assert guest.client.post("/account/mobile/confirm/", {"code": "000000"}).status_code == 200
    assert not VerifiedPhone.objects.exists()
    assert guest.client.post("/account/mobile/confirm/", {"code": CODE}).status_code == 302
    assert VerifiedPhone.objects.get().phone == number
    guest.booking.booking_intent.refresh_from_db()
    assert guest.booking.booking_intent.guest_phone == before
    if not has_booking:
        assert guest.booking.booking_intent.customer_id is None
    assert not Member.objects.exists() and not Ledger.objects.exists()


def test_binding_prefills_saved_phone_and_country_without_sending(guest, isolated):
    profile = profile_for(guest.user)
    profile.phone = "+12125551234"
    profile.save(update_fields=["phone"])
    page = guest.client.get("/account/mobile/link/")
    assert page.context["phone_form"]["phone"].value() == profile.phone
    assert page.context["phone_form"]["phone_country"].value() == "US"
    assert not PhoneChallenge.objects.exists() and not isolated.dispatched


@pytest.mark.parametrize(
    ("document", "expected"),
    [
        ({"status": {"accountStatus": {"status": "authenticated"}}}, True),
        ({"status": {"accountStatus": "authenticated"}}, True),
        ({"status": "authenticated"}, True),
        ({"status": {"accountStatus": {"status": "disconnected"}}}, False),
        ({"status": {"accountStatus": {"status": "qr"}}}, False),
        ({"status": {"accountStatus": {"status": True}}}, False),
        ({"status": {"accountStatus": {}}}, False),
        ({"status": True}, False),
        ({"accountStatus": {"status": "authenticated"}}, False),
        (None, False),
        (["authenticated"], False),
    ],
)
def test_provider_status_reads_real_nested_response_fail_closed(document, expected):
    assert auth._provider_authenticated(document) is expected


def test_brand_new_guest_registers_and_verifies_phone_without_any_booking(
    settings, isolated, django_capture_on_commit_callbacks
):
    settings.ACCOUNT_WHATSAPP_AUTH_EMAILS = ["new-guest@example.invalid"]
    client = Client()
    response = client.post(
        "/register/",
        {
            "first_name": "ضيف",
            "last_name": "جديد",
            "email": "new-guest@example.invalid",
            "accept_terms": "on",
        },
    )
    assert response.status_code == 302
    new_guest = get_user_model().objects.get(email="new-guest@example.invalid")
    profile = profile_for(new_guest)
    email_code = make_verification_code(new_guest.pk, new_guest.email, profile.verification_sent_at)
    assert client.post("/account/confirm/code/", {"code": email_code}).status_code == 302
    assert profile_for(new_guest).is_email_verified
    assert not Reservation.objects.exists()
    assert start(client, django_capture_on_commit_callbacks, binding=True).status_code == 302
    row = deliver(client)
    assert row.status == "sent" and len(isolated.sent) == 1
    assert client.post("/account/mobile/confirm/", {"code": CODE}).status_code == 302
    assert VerifiedPhone.objects.get().user_id == new_guest.pk
    assert not Reservation.objects.exists() and not Ledger.objects.exists()


@pytest.mark.parametrize("language", ["ar", "en", "fr"])
@pytest.mark.parametrize(
    ("status", "state"),
    [("pending", "waiting"), ("sending", "waiting"), ("sent", "accepted"), ("failed", "failed")],
)
def test_binding_shows_actual_delivery_state_not_account_eligibility(
    guest, isolated, language, status, state, settings, django_capture_on_commit_callbacks
):
    guest.client.cookies[settings.LANGUAGE_COOKIE_NAME] = language
    start(guest.client, django_capture_on_commit_callbacks, binding=True)
    row = PhoneChallenge.objects.get()
    row.status = status
    row.save(update_fields=["status"])
    before = (PhoneChallenge.objects.count(), VerifiedPhone.objects.count())
    response = guest.client.get("/account/mobile/confirm/")
    html = response.content.decode()
    assert TEXT[language][f"delivery_{state}"] in html
    assert TEXT[language]["sent"] not in html
    assert "phone-delivery.js" in html
    result = guest.client.get("/account/mobile/confirm/?delivery=1")
    assert result.json() == {"state": state}
    assert "no-store" in result["Cache-Control"]
    assert before == (PhoneChallenge.objects.count(), VerifiedPhone.objects.count())
    assert not isolated.sent
    assert len(isolated.dispatched) == 1  # Reading the status never sends again.


def test_binding_delivery_state_expires_and_never_verifies(
    guest, isolated, django_capture_on_commit_callbacks
):
    start(guest.client, django_capture_on_commit_callbacks, binding=True)
    isolated.clock[0] += timedelta(seconds=301)
    assert guest.client.get("/account/mobile/confirm/?delivery=1").json() == {"state": "expired"}
    assert not VerifiedPhone.objects.exists() and not isolated.sent


def test_delivery_status_is_not_an_anonymous_phone_lookup(
    guest, isolated, django_capture_on_commit_callbacks
):
    for known in (False, True):
        if known:
            VerifiedPhone.objects.create(user=guest.user, phone=PHONE)
        client = Client()
        isolated.clock[0] += timedelta(seconds=901)
        start(client, django_capture_on_commit_callbacks)
        deliver(client)
        assert client.get("/account/mobile/confirm/?delivery=1").status_code == 404
        html = client.get("/account/mobile/confirm/").content.decode()
        assert "phone-delivery.js" not in html
    assert Client().get("/account/mobile/confirm/?delivery=1").status_code == 404


def test_binding_status_is_bound_to_current_user_and_session(
    guest, django_capture_on_commit_callbacks
):
    start(guest.client, django_capture_on_commit_callbacks, binding=True)
    session = guest.client.session
    session["booking_session_marker"] = "another-session"
    session.save()
    assert guest.client.get("/account/mobile/confirm/?delivery=1").status_code == 404


@pytest.mark.parametrize("language", ["ar", "en", "fr"])
def test_phone_verification_entry_is_next_to_contact_number(guest, settings, language):
    guest.client.cookies[settings.LANGUAGE_COOKIE_NAME] = language
    html = guest.client.get("/my-bookings/details/").content.decode()
    assert html.count('href="/account/mobile/link/"') == 1
    assert TEXT[language]["verify_mobile"] in html
    assert TEXT[language]["unverified"] in html
    assert html.index('href="/account/mobile/link/"') < html.index('id="id_preferred_language"')


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


@pytest.mark.parametrize("language", ["ar", "en", "fr"])
def test_public_phone_verification_is_visible_to_non_allowlisted_guest(
    guest, settings, language, isolated
):
    settings.ACCOUNT_WHATSAPP_AUTH_PUBLIC = True
    settings.ACCOUNT_WHATSAPP_AUTH_EMAILS = []
    guest.client.cookies[settings.LANGUAGE_COOKIE_NAME] = language
    assert auth.ready() and auth.allowed(guest.user)
    page = guest.client.get("/my-bookings/details/")
    assert page.status_code == 200
    assert 'href="/account/mobile/link/"' in page.content.decode()
    assert TEXT[language]["verify_mobile"] in page.content.decode()
    assert guest.client.get("/account/mobile/link/").status_code == 200
    assert not isolated.sent and not isolated.dispatched


def test_public_guest_can_bind_without_booking_then_sign_in_with_one_use_code(
    guest, settings, isolated, django_capture_on_commit_callbacks
):
    settings.ACCOUNT_WHATSAPP_AUTH_PUBLIC = True
    settings.ACCOUNT_WHATSAPP_AUTH_EMAILS = []
    guest.booking.booking_intent.customer = None
    guest.booking.booking_intent.save(update_fields=["customer"])
    assert start(guest.client, django_capture_on_commit_callbacks, binding=True).status_code == 302
    assert deliver(guest.client).status == "sent"
    assert guest.client.post("/account/mobile/confirm/", {"code": CODE}).status_code == 302
    assert VerifiedPhone.objects.get(user=guest.user).phone == PHONE
    guest.client.logout()
    isolated.clock[0] += timedelta(seconds=61)
    assert start(guest.client, django_capture_on_commit_callbacks).status_code == 302
    login_code = deliver(guest.client)
    assert login_code.status == "sent"
    assert guest.client.post("/account/mobile/confirm/", {"code": CODE}).status_code == 302
    assert int(guest.client.session["_auth_user_id"]) == guest.user.pk
    login_code.refresh_from_db()
    assert login_code.status == "consumed" and not login_code.code_digest
    assert auth.PENDING not in guest.client.session
    assert not Member.objects.exists() and not Ledger.objects.exists()


@pytest.mark.parametrize("reason", ["inactive", "staff", "super", "owner", "manager", "unverified"])
def test_public_phone_keeps_identity_and_administration_guards(guest, settings, reason):
    settings.ACCOUNT_WHATSAPP_AUTH_PUBLIC = True
    settings.ACCOUNT_WHATSAPP_AUTH_EMAILS = []
    if reason == "inactive":
        guest.user.is_active = False
        guest.user.save()
    elif reason in {"staff", "super"}:
        setattr(guest.user, "is_staff" if reason == "staff" else "is_superuser", True)
        guest.user.save()
    elif reason == "owner":
        settings.OPERATIONS_OWNER_EMAIL = guest.user.email
    elif reason == "manager":
        OperationsManager.objects.create(email=guest.user.email, is_enabled=True)
    else:
        profile = profile_for(guest.user)
        profile.email_verified_at = None
        profile.save()
    assert not auth.allowed(guest.user)
    assert not PhoneChallenge.objects.exists() and not VerifiedPhone.objects.exists()


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
