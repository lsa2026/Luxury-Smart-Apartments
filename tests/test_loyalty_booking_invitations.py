"""Offline former-guest invitations; no real guest, provider or external writes."""

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from threading import Barrier
from unittest.mock import Mock

import pytest
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import close_old_connections, connection
from django.test import Client, RequestFactory
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import VerifiedPhone
from apps.loyalty import booking_invitations as invitations
from apps.loyalty.models import Ledger, Member, Referral
from apps.notifications.models import AuditLog
from apps.reservations.models import Reservation
from tests.test_account_booking_claim import make_reservation
from tests.test_loyalty_stage8 import join, user

pytestmark = pytest.mark.django_db
dispatch_request = invitations._dispatch


@pytest.fixture
def rig(settings, monkeypatch, client):
    settings.LOYALTY_ENABLED = settings.LOYALTY_UI_ENABLED = True
    settings.LOYALTY_UI_PREVIEW = settings.LOYALTY_PILOT_ONLY = False
    settings.LOYALTY_TERMS_APPROVED = True
    settings.LOYALTY_TERMS_VERSION = "synthetic-approved"
    settings.LOYALTY_TERMS_REFERENCE = "/synthetic-terms/"
    settings.LOYALTY_LAUNCH_AT = "2026-10-01T00:00:00+03:00"
    settings.ULTRAMSG_ENABLED = True
    settings.ULTRAMSG_INSTANCE_ID = settings.ULTRAMSG_TOKEN = "synthetic-only"
    settings.SITE_BASE_URL = "https://example.invalid"
    settings.EMAIL_DELIVERY_ENABLED = True
    settings.EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"
    settings.DEFAULT_FROM_EMAIL = "invitations@example.invalid"
    settings.HOSTAWAY_WEBSITE_RESERVATION_SYNC_ENABLED = False
    monkeypatch.setattr(timezone, "now", lambda: datetime(2026, 10, 22, 15, tzinfo=UTC))
    monkeypatch.setattr("httpx.Client.send", Mock(side_effect=AssertionError("Offline only")))
    owner = get_user_model().objects.create_superuser(username="invitation-owner")
    client.force_login(owner)
    dispatched = []
    monkeypatch.setattr(
        invitations,
        "_dispatch",
        lambda key, channel="whatsapp", request_id=None: dispatched.append(
            key if channel == "whatsapp" else (key, channel)
        ),
    )
    provider = Mock()
    provider.send_image.return_value = {"sent": True, "id": "synthetic-message"}
    provider.__enter__ = Mock(return_value=provider)
    provider.__exit__ = Mock(return_value=False)
    monkeypatch.setattr(invitations, "UltraMsgClient", Mock(return_value=provider))
    return client, owner, provider, dispatched


def past_booking(reference="SYNTHETIC-INVITE"):
    booking = make_reservation(reference)
    booking.normalized_status = Reservation.Status.CONFIRMED
    booking.source_type = Reservation.SourceType.DIRECT_WEBSITE
    booking.hostaway_reservation_id = 99000 + Reservation.objects.count()
    booking.check_in = timezone.now().date() - timedelta(days=4)
    booking.check_out = timezone.now().date() - timedelta(days=1)
    booking.save()
    booking.booking_intent.guest_phone = "+12025550123"
    booking.booking_intent.marketing_consent = True
    booking.booking_intent.language = "ar"
    booking.booking_intent.save()
    return booking


def url(booking):
    return reverse("notifications:booking_detail", args=[booking.pk])


def payload(client, booking, channel="whatsapp"):
    context = next(
        row
        for row in client.get(url(booking)).context["loyalty_invitation"]["channels"]
        if row["channel"] == channel
    )
    return {
        "action": "send_loyalty_invitation",
        "channel": channel,
        "confirm_loyalty_invitation": "on",
        "confirmation": context["confirmation"],
    }


def whatsapp_row(booking):
    return booking.loyalty_state[invitations.REQUESTS][booking.loyalty_state[invitations.LATEST]]


def email_row(booking):
    return booking.loyalty_state[invitations.EMAIL_REQUESTS][
        booking.loyalty_state[invitations.EMAIL_LATEST]
    ]


def deliver_latest(booking, channel="whatsapp"):
    booking.refresh_from_db()
    latest = invitations.LATEST if channel == "whatsapp" else invitations.EMAIL_LATEST
    return invitations.deliver_booking_invitation(
        str(booking.pk), channel, booking.loyalty_state[latest]
    )


def test_get_has_icon_confirmation_and_no_side_effects(rig):
    client, _, provider, dispatched = rig
    booking = past_booking()
    response = client.get(url(booking))
    html = response.content.decode()
    assert response.status_code == 200
    assert "دعوة لبرنامج الولاء" in html and '<svg viewBox="0 0 24 24"' in html
    assert 'data-booking-tab="loyalty"' in html
    assert 'data-booking-tab="overview"' not in html
    assert 'name="confirm_loyalty_invitation"' not in html
    assert html.count("معاينة نص الرسالة قبل الإرسال") == 2
    assert 'name="csrfmiddlewaretoken"' in html
    assert 'value="send_loyalty_invitation"' in html
    assert response.context["loyalty_invitation"]["phone"] == "+12025550123"
    booking.refresh_from_db()
    assert invitations.STATE not in booking.loyalty_state
    assert not dispatched and not provider.send_image.called
    assert not Member.objects.exists() and not Ledger.objects.exists()


def test_owner_can_choose_both_channels_without_saved_marketing_consent(rig):
    client, _, provider, dispatched = rig
    booking = past_booking()
    booking.booking_intent.marketing_consent = False
    booking.booking_intent.save()
    response = client.get(url(booking), {"section": "loyalty"})
    html = response.content.decode()
    assert 'data-booking-tab="loyalty"' in html
    assert response.context["detail_default_panel"] == "loyalty"
    assert "لا توجد موافقة محفوظة" not in html
    assert "إرسال الدعوة عبر واتساب" in html
    assert "إرسال الدعوة عبر البريد الإلكتروني" in html
    assert 'disabled aria-describedby="invitation-' not in html
    assert 'value="send_loyalty_invitation"' in html
    assert response.context["loyalty_invitation"]["confirmation"]
    assert not dispatched and not provider.send_image.called


@pytest.mark.parametrize("language", ["ar", "en", "fr"])
def test_preview_matches_delivered_message_without_sending_on_get(rig, mailoutbox, language):
    client, _, provider, dispatched = rig
    booking = past_booking()
    booking.booking_intent.language = language
    booking.booking_intent.save()
    page = client.get(url(booking))
    whatsapp, email = page.context["loyalty_invitation"]["channels"]
    assert not dispatched and not mailoutbox and not provider.send_image.called
    for channel, context in (("whatsapp", whatsapp), ("email", email)):
        client.post(
            url(booking),
            {
                "action": "send_loyalty_invitation",
                "channel": channel,
                "confirmation": context["confirmation"],
            },
        )
        deliver_latest(booking, channel)
    assert provider.send_image.call_args.kwargs["caption"] == whatsapp["preview_text"]
    assert mailoutbox[0].body == email["preview_text"]
    assert mailoutbox[0].subject == email["preview_subject"]


def test_owner_can_send_three_emails_and_legacy_history_is_preserved(rig, mailoutbox):
    client, _, _, _ = rig
    booking = past_booking()
    legacy = {"status": "accepted", "requested_at": timezone.now().isoformat()}
    booking.loyalty_state[invitations.EMAIL_STATE] = legacy.copy()
    booking.save()
    booking.booking_intent.marketing_consent = False
    booking.booking_intent.save()
    request_ids = []
    for _ in range(3):
        data = payload(client, booking, "email")
        data.pop("confirm_loyalty_invitation")
        client.post(url(booking), data)
        booking.refresh_from_db()
        request_ids.append(booking.loyalty_state[invitations.EMAIL_LATEST])
    for request_id in reversed(request_ids):
        invitations.deliver_booking_invitation(str(booking.pk), "email", request_id)
        invitations.deliver_booking_invitation(str(booking.pk), "email", request_id)
    booking.refresh_from_db()
    assert len(mailoutbox) == 3 and len(set(request_ids)) == 3
    assert booking.loyalty_state[invitations.EMAIL_STATE] == legacy
    assert all(
        row["status"] == "accepted"
        for row in booking.loyalty_state[invitations.EMAIL_REQUESTS].values()
    )
    assert not booking.booking_intent.marketing_consent


def test_legacy_email_task_can_still_finish_after_a_new_manual_request(rig, mailoutbox):
    client, owner, _, _ = rig
    booking = past_booking()
    booking.loyalty_state[invitations.EMAIL_STATE] = {
        "status": "queued",
        "requested_at": timezone.now().isoformat(),
        "operator": owner.pk,
        "target_digest": invitations.digest(invitations._target(booking, "email")),
    }
    booking.save()
    client.post(url(booking), payload(client, booking, "email"))
    invitations.deliver_booking_invitation(str(booking.pk), "email")
    invitations.deliver_booking_invitation(str(booking.pk), "email")
    deliver_latest(booking, "email")
    booking.refresh_from_db()
    assert len(mailoutbox) == 2
    assert booking.loyalty_state[invitations.EMAIL_STATE]["status"] == "accepted"
    assert email_row(booking)["status"] == "accepted"


def test_email_invitation_renders_and_sends_once_without_membership(
    rig, mailoutbox, django_capture_on_commit_callbacks
):
    client, _, provider, dispatched = rig
    booking = past_booking()
    data = payload(client, booking, "email")
    with django_capture_on_commit_callbacks(execute=True):
        client.post(url(booking), data)
        client.post(url(booking), data)
    assert dispatched == [(str(booking.pk), "email")]
    deliver_latest(booking, "email")
    deliver_latest(booking, "email")
    assert len(mailoutbox) == 1
    message = mailoutbox[0]
    assert message.to == [booking.booking_intent.guest_email]
    assert "https://example.invalid/my-bookings/rewards/" in message.body
    assert "أثر رجعي" in message.body and message.alternatives
    assert "دعوة" in message.subject
    booking.refresh_from_db()
    assert email_row(booking)["status"] == "accepted"
    assert not provider.send_image.called
    assert not Member.objects.exists() and not Ledger.objects.exists()


def test_each_channel_has_independent_proof_and_duplicate_guard(rig, mailoutbox):
    client, _, provider, _ = rig
    booking = past_booking()
    whatsapp = payload(client, booking)
    client.post(url(booking), dict(whatsapp, channel="email"))
    booking.refresh_from_db()
    assert not booking.loyalty_state.get(invitations.EMAIL_STATE)
    client.post(url(booking), whatsapp)
    client.post(url(booking), payload(client, booking, "email"))
    deliver_latest(booking)
    deliver_latest(booking, "email")
    deliver_latest(booking, "email")
    assert provider.send_image.call_count == 1 and len(mailoutbox) == 1


@pytest.mark.parametrize("change", ["consent", "address", "member"])
def test_email_worker_rechecks_address_without_overriding_owner_decision(rig, mailoutbox, change):
    client, _, provider, _ = rig
    booking = past_booking()
    client.post(url(booking), payload(client, booking, "email"))
    if change == "consent":
        booking.booking_intent.marketing_consent = False
    elif change == "address":
        booking.booking_intent.guest_email = "changed@example.invalid"
    else:
        guest = user("email-member")
        join(guest)
        booking.booking_intent.customer = guest
    booking.booking_intent.save()
    deliver_latest(booking, "email")
    booking.refresh_from_db()
    assert email_row(booking)["status"] == ("blocked" if change == "address" else "accepted")
    assert len(mailoutbox) == (0 if change == "address" else 1)
    assert not provider.send_image.called


@pytest.mark.parametrize("disabled", ["email", "whatsapp"])
def test_channels_have_independent_provider_readiness(rig, settings, disabled):
    client, _, _, _ = rig
    if disabled == "email":
        settings.EMAIL_DELIVERY_ENABLED = False
    else:
        settings.ULTRAMSG_ENABLED = False
    rows = client.get(url(past_booking())).context["loyalty_invitation"]["channels"]
    assert next(row for row in rows if row["channel"] == disabled).get("reason")
    assert next(row for row in rows if row["channel"] != disabled).get("confirmation")


@pytest.mark.parametrize("invalid", ["address", "console", "sender"])
def test_invalid_email_or_non_sending_backend_is_not_offered(rig, settings, invalid):
    client, _, _, _ = rig
    booking = past_booking()
    if invalid == "address":
        booking.booking_intent.guest_email = "invalid-address"
        booking.booking_intent.save()
    elif invalid == "console":
        settings.EMAIL_BACKEND = "django.core.mail.backends.console.EmailBackend"
    else:
        settings.DEFAULT_FROM_EMAIL = ""
    rows = client.get(url(booking)).context["loyalty_invitation"]["channels"]
    assert rows[1].get("reason") and not rows[1].get("confirmation")
    assert rows[0].get("confirmation")


def test_email_timeout_is_uncertain_and_never_automatically_retried(rig, monkeypatch):
    client, _, _, _ = rig
    booking = past_booking()
    client.post(url(booking), payload(client, booking, "email"))
    send = Mock(side_effect=TimeoutError("Synthetic outcome unknown"))
    monkeypatch.setattr(invitations.DjangoEmailProvider, "send", send)
    deliver_latest(booking, "email")
    deliver_latest(booking, "email")
    booking.refresh_from_db()
    assert email_row(booking)["status"] == "uncertain"
    assert send.call_count == 1


def test_owner_can_send_email_from_another_booking(rig, mailoutbox):
    client, _, _, _ = rig
    first, second = past_booking("EMAIL-ONE"), past_booking("EMAIL-TWO")
    second.booking_intent.guest_email = first.booking_intent.guest_email.upper()
    second.booking_intent.save()
    proof = payload(client, second, "email")
    client.post(url(first), payload(client, first, "email"))
    client.post(url(second), proof)
    deliver_latest(first, "email")
    deliver_latest(second, "email")
    assert len(mailoutbox) == 2


def test_confirmed_post_queues_after_commit_sends_once_and_changes_no_membership(
    rig, django_capture_on_commit_callbacks
):
    client, owner, provider, dispatched = rig
    booking = past_booking()
    data = payload(client, booking)
    before = (booking.total_price, booking.check_in, booking.check_out, booking.normalized_status)
    with django_capture_on_commit_callbacks(execute=True):
        response = client.post(url(booking), data)
        client.post(url(booking), data)
    assert response.status_code == 302 and dispatched == [str(booking.pk)]
    assert response.url.endswith("?section=loyalty")
    deliver_latest(booking)
    deliver_latest(booking)
    provider.send_image.assert_called_once()
    packet = provider.send_image.call_args.kwargs
    assert packet["recipient"] == booking.booking_intent.guest_phone
    assert packet["content"].startswith(b"\x89PNG")
    assert "https://example.invalid/my-bookings/rewards/" in packet["caption"]
    assert "invite=" not in packet["caption"] and "أثر رجعي" in packet["caption"]
    assert booking.public_reference not in packet["caption"]
    assert booking.booking_intent.guest_email not in packet["caption"]
    booking.refresh_from_db()
    assert whatsapp_row(booking)["status"] == "accepted"
    assert (
        booking.total_price,
        booking.check_in,
        booking.check_out,
        booking.normalized_status,
    ) == before
    assert AuditLog.objects.get(action="loyalty.booking_invitation_requested").actor_user == owner
    assert not any([Member.objects.exists(), Ledger.objects.exists(), Referral.objects.exists()])
    assert "الوصول والقراءة غير مؤكدين" in client.get(url(booking)).content.decode()


@pytest.mark.parametrize(
    "change", ["consent", "cancelled", "future", "phone", "member", "disabled", "address"]
)
def test_recheck_after_page_open_before_queue(rig, settings, change):
    client, _, provider, _ = rig
    booking = past_booking()
    data = payload(client, booking, "email")
    if change == "consent":
        booking.booking_intent.marketing_consent = False
        booking.booking_intent.save()
    elif change == "cancelled":
        booking.normalized_status = Reservation.Status.CANCELLED
        booking.save()
    elif change == "future":
        booking.check_out = timezone.now().date() + timedelta(days=1)
        booking.save()
    elif change == "phone":
        booking.booking_intent.guest_phone = "+12025550124"
        booking.booking_intent.save()
    elif change == "member":
        guest = user("already-member")
        join(guest)
        booking.booking_intent.customer = guest
        booking.booking_intent.save()
    elif change == "address":
        booking.booking_intent.guest_email = "changed@example.invalid"
        booking.booking_intent.save()
    else:
        settings.LOYALTY_ENABLED = False
    assert client.post(url(booking), data).status_code == 302
    booking.refresh_from_db()
    assert bool(booking.loyalty_state.get(invitations.EMAIL_REQUESTS)) == (
        change not in {"disabled", "address"}
    )
    assert not provider.send_image.called


@pytest.mark.parametrize("change", ["phone", "disabled", "operator", "expired"])
def test_worker_rechecks_before_network(rig, settings, change):
    client, owner, provider, _ = rig
    booking = past_booking()
    client.post(url(booking), payload(client, booking))
    if change == "consent":
        booking.booking_intent.marketing_consent = False
        booking.booking_intent.save()
    elif change == "phone":
        booking.booking_intent.guest_phone = "+12025550124"
        booking.booking_intent.save()
    elif change == "joined":
        guest = user("verified-recipient")
        join(guest)
        VerifiedPhone.objects.create(user=guest, phone=booking.booking_intent.guest_phone)
    elif change == "disabled":
        settings.LOYALTY_ENABLED = False
    elif change == "operator":
        owner.is_active = False
        owner.save()
    else:
        booking.refresh_from_db()
        whatsapp_row(booking)["requested_at"] = (timezone.now() - timedelta(minutes=16)).isoformat()
        booking.save()
    deliver_latest(booking)
    booking.refresh_from_db()
    assert whatsapp_row(booking)["status"] == "blocked"
    assert not provider.send_image.called


def test_owner_can_invite_same_phone_from_another_booking(rig):
    client, _, provider, _ = rig
    first, second = past_booking("PAST-ONE"), past_booking("PAST-TWO")
    data = payload(client, second)
    client.post(url(first), payload(client, first))
    client.post(url(second), data)
    deliver_latest(first)
    deliver_latest(second)
    assert provider.send_image.call_count == 2


def test_owner_can_send_three_independent_requests_without_marketing_consent(rig):
    client, _, provider, _ = rig
    booking = past_booking()
    booking.booking_intent.marketing_consent = False
    booking.booking_intent.save()
    request_ids = []
    for _ in range(3):
        data = payload(client, booking)
        data.pop("confirm_loyalty_invitation")
        client.post(url(booking), data)
        booking.refresh_from_db()
        request_ids.append(booking.loyalty_state[invitations.LATEST])
    for request_id in reversed(request_ids):
        invitations.deliver_booking_invitation(str(booking.pk), "whatsapp", request_id)
        invitations.deliver_booking_invitation(str(booking.pk), "whatsapp", request_id)
    booking.refresh_from_db()
    assert provider.send_image.call_count == 3
    assert len(set(request_ids)) == 3
    assert all(
        row["status"] == "accepted" for row in booking.loyalty_state[invitations.REQUESTS].values()
    )
    assert len({call.kwargs["reference_id"] for call in provider.send_image.call_args_list}) == 3
    assert not booking.booking_intent.marketing_consent
    assert not any([Member.objects.exists(), Ledger.objects.exists(), Referral.objects.exists()])


@pytest.mark.parametrize("channel", ["whatsapp", "email"])
@pytest.mark.parametrize("state", ["member", "future", "cancelled"])
def test_manual_send_is_an_owner_decision(rig, mailoutbox, channel, state):
    client, _, provider, _ = rig
    booking = past_booking()
    if state == "member":
        guest = user("manual-recipient")
        join(guest)
        booking.booking_intent.customer = guest
        booking.booking_intent.save()
    elif state == "future":
        booking.check_in += timedelta(days=10)
        booking.check_out += timedelta(days=10)
        booking.save()
    else:
        booking.normalized_status = Reservation.Status.CANCELLED
        booking.save()
    client.post(url(booking), payload(client, booking, channel))
    deliver_latest(booking, channel)
    assert provider.send_image.call_count == (1 if channel == "whatsapp" else 0)
    assert len(mailoutbox) == (1 if channel == "email" else 0)


def test_prior_or_uncertain_whatsapp_request_never_blocks_a_fresh_manual_send(rig):
    client, _, provider, _ = rig
    booking = past_booking()
    legacy = {"status": "accepted", "requested_at": timezone.now().isoformat()}
    booking.loyalty_state[invitations.STATE] = legacy.copy()
    booking.save()
    client.post(url(booking), payload(client, booking))
    provider.send_image.side_effect = TimeoutError("Synthetic unknown outcome")
    deliver_latest(booking)
    booking.refresh_from_db()
    first_id = booking.loyalty_state[invitations.LATEST]
    assert whatsapp_row(booking)["status"] == "uncertain"
    client.post(url(booking), payload(client, booking))
    provider.send_image.side_effect = None
    deliver_latest(booking)
    invitations.deliver_booking_invitation(str(booking.pk), "whatsapp", first_id)
    booking.refresh_from_db()
    assert provider.send_image.call_count == 2
    assert booking.loyalty_state[invitations.STATE] == legacy
    assert whatsapp_row(booking)["status"] == "accepted"


@pytest.mark.parametrize("failure", ["exception", "missing_reference"])
def test_uncertain_provider_outcome_never_retries(rig, failure):
    client, _, provider, _ = rig
    booking = past_booking()
    client.post(url(booking), payload(client, booking))
    if failure == "exception":
        provider.send_image.side_effect = TimeoutError("Synthetic timeout")
    else:
        provider.send_image.return_value = {"sent": True}
    deliver_latest(booking)
    deliver_latest(booking)
    booking.refresh_from_db()
    assert whatsapp_row(booking)["status"] == "uncertain"
    assert provider.send_image.call_count == 1


def test_whatsapp_signature_required_but_owner_needs_no_consent_checkbox(rig):
    client, _, provider, _ = rig
    booking = past_booking()
    data = payload(client, booking)
    client.post(url(booking), dict(data, confirmation="tampered"))
    booking.refresh_from_db()
    assert invitations.REQUESTS not in booking.loyalty_state and not provider.send_image.called
    client.post(url(booking), dict(data, confirm_loyalty_invitation=""))
    deliver_latest(booking)
    assert provider.send_image.call_count == 1


def test_guest_forbidden_and_csrf_enforced(rig):
    client, owner, provider, _ = rig
    booking = past_booking()
    data = payload(client, booking)
    client.force_login(user("ordinary-guest"))
    assert client.post(url(booking), data).status_code == 403
    csrf_client = Client(enforce_csrf_checks=True)
    csrf_client.force_login(owner)
    assert csrf_client.post(url(booking), data).status_code == 403
    assert not provider.send_image.called


def test_membership_identified_by_verified_phone_does_not_block_manual_invitation(rig):
    client, _, provider, _ = rig
    guest = user("existing-member")
    join(guest)
    booking = past_booking()
    VerifiedPhone.objects.create(user=guest, phone=booking.booking_intent.guest_phone)
    page = client.get(url(booking))
    assert all(row["confirmation"] for row in page.context["loyalty_invitation"]["channels"])
    assert not provider.send_image.called


@pytest.mark.parametrize("language", ["ar", "en", "fr"])
def test_single_language_invitation_with_approved_logo(rig, language):
    client, _, provider, _ = rig
    booking = past_booking()
    booking.booking_intent.language = language
    booking.booking_intent.save()
    client.post(url(booking), payload(client, booking))
    deliver_latest(booking)
    caption = provider.send_image.call_args.kwargs["caption"]
    expected = {"ar": "موافقتك", "en": "free and optional", "fr": "gratuite et facultative"}
    assert expected[language] in caption
    assert all(expected[other] not in caption for other in expected if other != language)
    assert len(caption.encode("utf-16-le")) // 2 <= 1024


def test_prelaunch_booking_can_be_invited_but_no_retroactive_points(rig):
    client, _, provider, _ = rig
    booking = past_booking()
    Reservation.objects.filter(pk=booking.pk).update(created_at=datetime(2026, 9, 1, tzinfo=UTC))
    client.post(url(booking), payload(client, booking))
    deliver_latest(booking)
    assert provider.send_image.call_count == 1
    assert not Member.objects.exists() and not Ledger.objects.exists()


def test_queue_failure_does_not_fall_back_to_direct_send(
    rig, monkeypatch, django_capture_on_commit_callbacks
):
    client, _, provider, _ = rig
    booking = past_booking()
    monkeypatch.setattr(invitations, "_dispatch", dispatch_request)
    monkeypatch.setattr(invitations.deliver_booking_invitation, "delay", Mock(side_effect=OSError))
    with django_capture_on_commit_callbacks(execute=True):
        client.post(url(booking), payload(client, booking))
    booking.refresh_from_db()
    assert whatsapp_row(booking)["status"] == "queue_failed"
    assert not provider.send_image.called


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize(
    "channel,mode",
    [
        ("whatsapp", "different"),
        ("email", "different"),
        ("whatsapp", "same_request"),
        ("whatsapp", "same_booking"),
        ("email", "same_request"),
        ("email", "same_booking"),
    ],
)
def test_postgres_concurrent_manual_requests(rig, channel, mode):
    if connection.vendor != "postgresql":
        pytest.skip("Requires independent PostgreSQL connections and advisory locks.")
    client, owner, provider, dispatched = rig
    bookings = [past_booking("CONCURRENT-ONE"), past_booking("CONCURRENT-TWO")]
    if mode != "different":
        bookings[1] = bookings[0]
    requests = []
    first_data = payload(client, bookings[0], channel)
    for booking in bookings:
        data = first_data if mode == "same_request" else payload(client, booking, channel)
        request = RequestFactory().post(url(booking), data)
        request.user = owner
        requests.append((request, booking.pk))
    barrier = Barrier(2)

    def run(item):
        request, booking_id = item
        close_old_connections()
        try:
            barrier.wait(timeout=10)
            try:
                invitations.queue_booking_invitation(request, booking_id)
            except ValidationError:
                return "duplicate"
            return "queued"
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(run, requests))
    expected_count = 2 if mode != "same_request" else 1
    assert results == ["queued", "queued"]
    assert len(dispatched) == expected_count and not provider.send_image.called
    assert (
        AuditLog.objects.filter(action="loyalty.booking_invitation_requested").count()
        == expected_count
    )
