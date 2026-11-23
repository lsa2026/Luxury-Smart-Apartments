"""Synthetic OTPs only; private source and bridge are exercised without networks."""

import uuid
from datetime import timedelta
from unittest.mock import Mock

import pytest
from django.test import Client
from django.utils import timezone

from apps.accounts import phone_auth as auth
from apps.accounts import phone_workgroup as service
from apps.accounts.models import PhoneChallenge, VerifiedPhone
from apps.reservations import telegram_authority as authority
from apps.reservations import telegram_website_workgroup as transport
from apps.reservations.telegram_stay_models import TelegramWebsiteHandoff
from tests import test_guest_phone_auth as legacy
from tests.test_website_workgroup import proof

pytestmark = pytest.mark.django_db
isolated = legacy.isolated
guest = legacy.guest


@pytest.fixture(autouse=True)
def manual(settings, isolated, monkeypatch):
    settings.TELEGRAM_WORKGROUP_ONLY_POLICY = True
    settings.TELEGRAM_WORKGROUP_HANDOFF_ENABLED = True
    settings.ACCOUNT_WORKGROUP_AUTH_ENABLED = True
    settings.ACCOUNT_WORKGROUP_AUTH_ONLY_POLICY = True
    settings.ACCOUNT_WORKGROUP_AUTH_CUTOVER_AT = (timezone.now() - timedelta(seconds=1)).isoformat()
    settings.ACCOUNT_WHATSAPP_AUTH_PUBLIC = True
    # These historical credentials cannot be a readiness dependency or fallback.
    settings.ULTRAMSG_ENABLED = False
    settings.ULTRAMSG_TOKEN = ""
    settings.ULTRAMSG_INSTANCE_ID = ""
    forbidden = Mock(side_effect=AssertionError("legacy OTP provider is forbidden"))
    monkeypatch.setattr(auth, "UltraMsgClient", forbidden)
    monkeypatch.setattr(
        TelegramWebsiteHandoff._meta.get_field("created_at"), "_get_default", lambda: timezone.now()
    )
    monkeypatch.setattr(
        TelegramWebsiteHandoff._meta.get_field("next_probe_at"),
        "_get_default",
        lambda: timezone.now(),
    )
    monkeypatch.setattr(
        PhoneChallenge._meta.get_field("created_at"), "_get_default", lambda: timezone.now()
    )
    return forbidden


@pytest.fixture
def bridge(monkeypatch):
    calls, packets = [], []

    class Backend:
        authority_context = None

        def call(self, action, key):
            row = TelegramWebsiteHandoff.objects.get(pk=key)
            envelope = authority.mint(self.authority_context, action, key)
            value = authority.authorize(dict(envelope, contract="tg39_activation_readback_v1"))
            calls.append(action)
            packets.append(value["packet"])
            return proof(row)

        def close(self):
            pass

    monkeypatch.setattr(transport, "Backend", Backend)
    return calls, packets


def begin(guest, callbacks):
    response = legacy.start(guest.client, callbacks, binding=True)
    assert response.status_code == 302
    return PhoneChallenge.objects.get(pk=guest.client.session[auth.PENDING])


def test_group_acceptance_needs_correct_same_session_consume(
    guest, bridge, manual, django_capture_on_commit_callbacks
):
    challenge = begin(guest, django_capture_on_commit_callbacks)
    assert not challenge.code_digest and not challenge.workgroup_nonce
    auth.deliver_phone_code(challenge.pk)
    challenge.refresh_from_db()
    assert challenge.status == "handed_off" and challenge.workgroup_authorized_at
    assert not VerifiedPhone.objects.exists() and not manual.called
    row = TelegramWebsiteHandoff.objects.get()
    assert row.pk == challenge.pk and row.job_key == "site01otp:" + str(row.pk)
    assert row.state == "handed_off" and row.source_kind == "phone_code"
    value = bridge[1][0]
    assert value["contract"] == "tg39_phone_code_packet_v1"
    code = service.code_for(challenge)
    assert code in value["caption_html"]
    assert value["purpose"] == "phone_bind" and value["asset_base64"]
    assert value["expires_at"] == challenge.expires_at.isoformat()
    assert Client().post("/account/mobile/confirm/", {"code": code}).status_code == 302
    assert not VerifiedPhone.objects.exists()
    # The anonymous visitor cannot consume another session's token.
    response = guest.client.post("/account/mobile/confirm/", {"code": code})
    assert response.status_code == 302 and response.url == "/my-bookings/details/"
    assert VerifiedPhone.objects.get().user_id == guest.user.pk
    challenge.refresh_from_db()
    assert challenge.status == "consumed" and not challenge.code_digest
    assert guest.client.post("/account/mobile/confirm/", {"code": code}).status_code == 302
    assert VerifiedPhone.objects.count() == 1
    saved = str(list(TelegramWebsiteHandoff.objects.values()))
    assert "caption_html" not in saved and "asset_base64" not in saved and legacy.PHONE not in saved
    # Group metadata cannot be used on the ordinary source/body endpoint.
    with pytest.raises(authority.Denied):
        transport.authorize({"job": str(row.pk)}, "website_handoff", str(row.pk), {})


def test_one_use_private_source_and_duplicate_task_never_regenerate_or_post(
    guest, bridge, django_capture_on_commit_callbacks
):
    challenge = begin(guest, django_capture_on_commit_callbacks)
    auth.deliver_phone_code(challenge.pk)
    challenge.refresh_from_db()
    nonce, digest = challenge.workgroup_nonce, challenge.code_digest
    auth.deliver_phone_code(challenge.pk)
    challenge.refresh_from_db()
    assert challenge.workgroup_nonce == nonce and challenge.code_digest == digest
    assert bridge[0] == ["phone_code_handoff"]
    with pytest.raises(authority.Denied):
        service.authorize({"job": str(challenge.pk)}, "phone_code_handoff", str(challenge.pk), {})


@pytest.mark.parametrize(
    "case", ["flag", "paused", "both_group_flags_off", "future_cutover", "missing_cutover"]
)
def test_manual_pause_is_closed_and_has_no_ultramsg_fallback(case, settings, manual):
    if case == "flag":
        settings.ACCOUNT_WORKGROUP_AUTH_ENABLED = False
    elif case == "paused":
        settings.TELEGRAM_WORKGROUP_HANDOFF_ENABLED = False
    elif case == "both_group_flags_off":
        settings.TELEGRAM_WORKGROUP_HANDOFF_ENABLED = False
        settings.TELEGRAM_WORKGROUP_ONLY_POLICY = False
        settings.ULTRAMSG_ENABLED = True
        settings.ULTRAMSG_INSTANCE_ID = "historical-test-only"
        settings.ULTRAMSG_TOKEN = "historical-test-only"
    elif case == "future_cutover":
        settings.ACCOUNT_WORKGROUP_AUTH_CUTOVER_AT = (
            timezone.now() + timedelta(seconds=10)
        ).isoformat()
    else:
        settings.ACCOUNT_WORKGROUP_AUTH_CUTOVER_AT = ""
    assert not auth.ready()
    assert Client().get("/account/mobile/").status_code == 404
    auth.deliver_phone_code(uuid.uuid4())
    assert not manual.called and not TelegramWebsiteHandoff.objects.exists()


def test_pending_before_cutover_is_not_replayed(
    guest, settings, bridge, django_capture_on_commit_callbacks
):
    challenge = begin(guest, django_capture_on_commit_callbacks)
    settings.ACCOUNT_WORKGROUP_AUTH_CUTOVER_AT = (
        challenge.created_at + timedelta(seconds=1)
    ).isoformat()
    auth.deliver_phone_code(challenge.pk)
    assert not bridge[0] and not TelegramWebsiteHandoff.objects.exists()
    challenge.refresh_from_db()
    assert challenge.status == "pending" and not challenge.workgroup_nonce


@pytest.mark.parametrize("case", ["expired", "superseded", "disabled_user", "phone_claimed"])
def test_source_changes_before_task_cannot_release_a_code(
    case, guest, bridge, isolated, django_capture_on_commit_callbacks
):
    challenge = begin(guest, django_capture_on_commit_callbacks)
    if case == "expired":
        isolated.clock[0] = challenge.expires_at
    elif case == "superseded":
        challenge.status = "superseded"
        challenge.save(update_fields=["status"])
    elif case == "disabled_user":
        guest.user.is_active = False
        guest.user.save(update_fields=["is_active"])
    else:
        VerifiedPhone.objects.create(user=guest.user, phone=legacy.PHONE)
    auth.deliver_phone_code(challenge.pk)
    assert not bridge[0] and not TelegramWebsiteHandoff.objects.exists()


def test_unknown_recovery_is_status_only_even_after_expiry(
    guest, isolated, monkeypatch, django_capture_on_commit_callbacks
):
    calls, seen = [], []
    accepted_at = timezone.now().isoformat()

    class Backend:
        authority_context = None

        def call(self, action, key):
            calls.append(action)
            value = service.authorize(self.authority_context, action, key, {})["packet"]
            row = TelegramWebsiteHandoff.objects.get(pk=key)
            if action == "phone_code_handoff":
                seen.append(value)
                raise transport.Unavailable
            assert set(value) == {"contract", *transport.metadata(row)}
            assert "caption_html" not in value and "guest_phone_e164" not in value
            return proof(row, accepted_at=accepted_at)

        def close(self):
            pass

    monkeypatch.setattr(transport, "Backend", Backend)
    challenge = begin(guest, django_capture_on_commit_callbacks)
    auth.deliver_phone_code(challenge.pk)
    row = TelegramWebsiteHandoff.objects.get()
    assert row.state == "uncertain" and seen
    challenge.refresh_from_db()
    assert not VerifiedPhone.objects.exists()
    auth.deliver_phone_code(challenge.pk)
    assert calls == ["phone_code_handoff"]
    isolated.clock[0] = challenge.expires_at + timedelta(seconds=1)
    transport.recover()
    row.refresh_from_db()
    assert row.state == "handed_off" and calls == [
        "phone_code_handoff",
        "phone_code_handoff_status",
    ]
    assert (
        guest.client.post(
            "/account/mobile/confirm/", {"code": service.code_for(challenge)}
        ).status_code
        == 200
    )
    assert not VerifiedPhone.objects.exists()


def test_hand_off_does_not_remove_attempt_limit(guest, bridge, django_capture_on_commit_callbacks):
    challenge = begin(guest, django_capture_on_commit_callbacks)
    auth.deliver_phone_code(challenge.pk)
    for _ in range(5):
        guest.client.post("/account/mobile/confirm/", {"code": "invalid"})
    challenge.refresh_from_db()
    assert challenge.status == "locked" and challenge.attempts == 5 and not challenge.code_digest
    assert not VerifiedPhone.objects.exists()


def test_login_still_requires_existing_binding_and_same_browser(
    guest, bridge, django_capture_on_commit_callbacks
):
    binding = VerifiedPhone.objects.create(user=guest.user, phone=legacy.PHONE)
    client = Client()
    assert legacy.start(client, django_capture_on_commit_callbacks).status_code == 302
    challenge = PhoneChallenge.objects.get(pk=client.session[auth.PENDING])
    auth.deliver_phone_code(challenge.pk)
    challenge.refresh_from_db()
    assert bridge[1][0]["purpose"] == "phone_login"
    code = service.code_for(challenge)
    binding.version = uuid.uuid4()
    binding.save(update_fields=["version"])
    assert client.post("/account/mobile/confirm/", {"code": code}).status_code == 200
    assert "_auth_user_id" not in client.session


def test_valid_login_receipt_does_not_log_in_without_code(
    guest, bridge, django_capture_on_commit_callbacks
):
    VerifiedPhone.objects.create(user=guest.user, phone=legacy.PHONE)
    client = Client()
    legacy.start(client, django_capture_on_commit_callbacks)
    challenge = PhoneChallenge.objects.get(pk=client.session[auth.PENDING])
    auth.deliver_phone_code(challenge.pk)
    challenge.refresh_from_db()
    assert "_auth_user_id" not in client.session
    response = client.post("/account/mobile/confirm/", {"code": service.code_for(challenge)})
    assert response.status_code == 302
    assert int(client.session["_auth_user_id"]) == guest.user.pk


def test_proof_cannot_adopt_an_attempt_before_source_release(
    guest, monkeypatch, django_capture_on_commit_callbacks
):
    monkeypatch.setattr(transport, "call", lambda *args: None)
    challenge = begin(guest, django_capture_on_commit_callbacks)
    auth.deliver_phone_code(challenge.pk)
    row = TelegramWebsiteHandoff.objects.get()
    with pytest.raises(authority.Denied):
        transport.import_proof(row, proof(row))
    assert not VerifiedPhone.objects.exists()


def test_unknown_attempt_is_pinned_and_cannot_be_replaced(
    guest, monkeypatch, django_capture_on_commit_callbacks
):
    attempt = str(uuid.uuid4())

    class Backend:
        authority_context = None

        def call(self, action, key):
            service.authorize(self.authority_context, action, key, {})
            return proof(
                TelegramWebsiteHandoff.objects.get(pk=key),
                attempt_id=attempt,
                status="unknown",
                api_accepted=False,
            )

        def close(self):
            pass

    monkeypatch.setattr(transport, "Backend", Backend)
    challenge = begin(guest, django_capture_on_commit_callbacks)
    auth.deliver_phone_code(challenge.pk)
    row = TelegramWebsiteHandoff.objects.get()
    assert row.state == "uncertain" and str(row.attempt_id) == attempt
    with pytest.raises(authority.Denied):
        transport.import_proof(row, proof(row))
    assert not VerifiedPhone.objects.exists()


@pytest.mark.parametrize("case", ["one_part", "duplicate_part_ids", "wrong_phone", "guest_flag"])
def test_receipt_requires_two_parts_exact_identity_and_no_guest_delivery(
    case, guest, bridge, django_capture_on_commit_callbacks
):
    challenge = begin(guest, django_capture_on_commit_callbacks)
    auth.deliver_phone_code(challenge.pk)
    row = TelegramWebsiteHandoff.objects.get()
    changes = {"attempt_id": str(row.attempt_id)}
    if case == "one_part":
        changes.update(provider_message_ids=["102"], provider_message_id="102")
    elif case == "duplicate_part_ids":
        changes.update(provider_message_ids=["102", "102"], provider_message_id="102")
    elif case == "wrong_phone":
        changes.update(phone_hash="0" * 64)
    else:
        changes.update(guest_accepted=True)
    with pytest.raises(authority.Denied):
        transport.import_proof(row, proof(row, **changes))


def test_late_receipt_records_handoff_but_cannot_extend_code_expiry(
    guest, bridge, isolated, django_capture_on_commit_callbacks
):
    challenge = begin(guest, django_capture_on_commit_callbacks)
    auth.deliver_phone_code(challenge.pk)
    challenge.refresh_from_db()
    row = TelegramWebsiteHandoff.objects.get()
    isolated.clock[0] = challenge.expires_at + timedelta(seconds=1)
    assert transport.import_proof(row, proof(row, attempt_id=str(row.attempt_id)))
    assert (
        guest.client.post(
            "/account/mobile/confirm/",
            {"code": service.code_for(challenge)},
        ).status_code
        == 200
    )
    assert not VerifiedPhone.objects.exists()


def test_new_request_supersedes_handed_off_code_without_replaying_it(
    guest, bridge, isolated, django_capture_on_commit_callbacks
):
    old = begin(guest, django_capture_on_commit_callbacks)
    auth.deliver_phone_code(old.pk)
    isolated.clock[0] += timedelta(seconds=61)
    new = begin(guest, django_capture_on_commit_callbacks)
    old.refresh_from_db()
    assert old.status == "superseded" and not old.code_digest
    assert old.pk != new.pk and not new.workgroup_nonce
    auth.deliver_phone_code(old.pk)
    assert bridge[0] == ["phone_code_handoff"]


@pytest.mark.parametrize("language", ["ar", "en", "fr"])
def test_manual_ui_describes_handoff_without_exposing_code(
    language, guest, bridge, settings, django_capture_on_commit_callbacks
):
    guest.client.cookies[settings.LANGUAGE_COOKIE_NAME] = language
    challenge = begin(guest, django_capture_on_commit_callbacks)
    auth.deliver_phone_code(challenge.pk)
    challenge.refresh_from_db()
    response = guest.client.get("/account/mobile/confirm/")
    assert service.code_for(challenge) not in response.content.decode()
    assert response.context["phone_delivery"] == "accepted"
    assert response.context["phone_copy"]["send"] != legacy.TEXT[language]["send"]
    assert "no-store" in response["Cache-Control"]
