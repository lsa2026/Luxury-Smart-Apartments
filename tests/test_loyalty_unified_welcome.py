"""Fictional guests and offline providers: seven entrances, one consent and welcome."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from django.core import mail
from django.test import Client, RequestFactory
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import VerifiedPhone, profile_for
from apps.loyalty import welcome, whatsapp_notices
from apps.loyalty.booking_invitations import invitation_caption, invitation_email_request
from apps.loyalty.events import record
from apps.loyalty.invitation_links import invitation_url
from apps.loyalty.models import GuestEvent, Ledger, Member, MovementNotice, Referral
from apps.loyalty.phone_gifts import invitation_message
from apps.notifications.models import EmailDelivery
from apps.notifications.services.email import send_queued_email
from tests.test_loyalty_stage8 import completed_inviter, join, user

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def programme(settings, monkeypatch):
    settings.LOYALTY_ENABLED = settings.LOYALTY_UI_ENABLED = True
    settings.LOYALTY_TERMS_APPROVED = settings.EMAIL_DELIVERY_ENABLED = True
    settings.LOYALTY_PILOT_ONLY = settings.LOYALTY_UI_PREVIEW = False
    settings.LOYALTY_MESSAGES_ENABLED = False  # Transactional, not a promotional campaign.
    settings.LOYALTY_WHATSAPP_NOTICES_ENABLED = True
    settings.LOYALTY_TERMS_VERSION = "fictional-approved-v1"
    settings.LOYALTY_TERMS_REFERENCE = "/fictional-terms/"
    settings.LOYALTY_LAUNCH_AT = "2026-10-01T00:00:00+03:00"
    settings.SITE_BASE_URL = "https://example.invalid"
    clock = [datetime(2026, 10, 10, 13, tzinfo=UTC)]
    monkeypatch.setattr(timezone, "now", lambda: clock[0])
    monkeypatch.setattr(whatsapp_notices, "_dispatch", Mock())
    return clock


@pytest.mark.parametrize(
    "entrance",
    ["menu", "direct", "referral", "gift-invitation", "booking-whatsapp", "booking-email", "terms"],
)
def test_seven_entrances_share_consent_and_exactly_one_welcome_per_channel(entrance, programme):
    inviter, token = completed_inviter(user("inviter"), programme)
    inviter.refresh_from_db()
    guest = user("new-guest")
    VerifiedPhone.objects.create(user=guest, phone="+12025550100")
    client = Client()
    client.force_login(guest)
    target = reverse("accounts:rewards")
    referral = entrance in {"referral", "gift-invitation"}
    if referral:
        link = invitation_url(RequestFactory().get("/"), inviter)
        if entrance == "gift-invitation":
            assert link in invitation_message(inviter, language="ar", url=link)
        target = client.get(link.removeprefix("http://testserver")).url
        assert token in target
    elif entrance == "menu":
        assert f'href="{target}"' in client.get(reverse("accounts:dashboard")).content.decode()
    elif entrance == "terms":
        terms = reverse("core:rewards_terms", kwargs={"version": "2026-10-18-v5"})
        assert f'href="{target}"' in client.get(terms).content.decode()
    elif entrance == "booking-whatsapp":
        assert settings_url(target) in invitation_caption("ar", settings_url(target))
    elif entrance == "booking-email":
        request = invitation_email_request(
            {"language": "ar", "url": settings_url(target), "email": guest.email}
        )
        assert request.context["action_url"] == settings_url(target)
    page = client.get(target)
    assert page.status_code == 200 and page.context["loyalty_member"] is None
    assert not EmailDelivery.objects.filter(
        recipient_source="loyalty_welcome", recipient_masked__contains="new-guest"
    ).exists()
    payload = {"action": "join"}
    assert client.post(target, payload).status_code == 200
    assert not Member.objects.filter(customer=guest).exists()
    payload["consent"] = "on"
    if referral:
        assert client.post(target, payload).status_code == 200
        payload["confirm_inviter"] = "on"
    assert client.post(target, payload).status_code == 302
    assert client.post(target, payload).status_code == 200
    member = Member.objects.get(customer=guest)
    event = GuestEvent.objects.get(customer=guest, kind=GuestEvent.Kind.JOIN)
    assert member.terms_version == "fictional-approved-v1" and len(member.consent_history) == 1
    assert Referral.objects.get(member=member).inviter_id == (inviter.pk if referral else None)
    assert not Ledger.objects.filter(member=member).exists()
    assert MovementNotice.objects.filter(event=event).count() == 1
    delivery = EmailDelivery.objects.get(
        recipient_source="loyalty_welcome", recipient_reference=str(event.pk)
    )
    assert welcome.queue(event).pk == delivery.pk
    assert send_queued_email(delivery.pk).sent
    assert not send_queued_email(delivery.pk).sent
    assert len(mail.outbox) == 1
    assert member.number in mail.outbox[0].body
    assert member.number in mail.outbox[0].alternatives[0].content
    assert "welcome-ar-v1.png" in mail.outbox[0].alternatives[0].content


def settings_url(path):
    return "https://example.invalid" + path


def test_each_channel_waits_for_its_verified_identity_without_changing_membership():
    guest = user("email-only")
    member = join(guest)
    assert EmailDelivery.objects.filter(message_type="loyalty_welcome").count() == 1
    notice = MovementNotice.objects.get(event__member=member)
    assert notice.status == "skipped" and notice.error_code == "verified_mobile_required"
    VerifiedPhone.objects.create(user=guest, phone="+12025550101")
    notice.refresh_from_db()
    assert notice.status == "queued"
    phone_guest = user("phone-only", verified=False)
    profile_for(phone_guest)
    VerifiedPhone.objects.create(user=phone_guest, phone="+12025550102")
    phone_member = join(phone_guest)
    assert MovementNotice.objects.get(event__member=phone_member).status == "queued"
    assert EmailDelivery.objects.count() == 1
    profile_for(phone_guest).mark_verified()
    profile_for(phone_guest).mark_verified()
    assert EmailDelivery.objects.count() == 2


@pytest.mark.parametrize("change", ["email", "verification", "closed", "inactive", "disabled"])
def test_welcome_rechecks_recipient_and_membership_before_sending(change, settings):
    guest = user("changed")
    member = join(guest)
    delivery = EmailDelivery.objects.get(message_type="loyalty_welcome")
    if change == "email":
        guest.email = "different@example.invalid"
        guest.save()
    elif change == "verification":
        profile = profile_for(guest)
        profile.email_verified_at = None
        profile.save()
    elif change == "closed":
        member.status = "withdrawn"
        member.save()
    elif change == "inactive":
        guest.is_active = False
        guest.save()
    else:
        settings.LOYALTY_ENABLED = False
    assert not send_queued_email(delivery.pk).sent and not mail.outbox


def test_no_history_backfill_or_welcome_from_account_only(programme):
    guest = user("account-only")
    assert not EmailDelivery.objects.exists() and not MovementNotice.objects.exists()
    member = join(guest)
    before = EmailDelivery.objects.count()
    record(
        customer=guest,
        member=member,
        kind=GuestEvent.Kind.JOIN,
        reference="history-join",
        source="history",
    )
    assert EmailDelivery.objects.count() == before
    phone_guest = user("old-unverified", verified=False)
    VerifiedPhone.objects.create(user=phone_guest, phone="+12025550103")
    join(phone_guest)
    programme[0] += timedelta(days=2)
    profile_for(phone_guest).mark_verified()
    assert EmailDelivery.objects.count() == before


@pytest.mark.parametrize("language", ["ar", "en", "fr"])
def test_both_channels_share_copy_language_and_packaged_artwork(language):
    guest = user("localized")
    member = join(guest, language=language)
    delivery = EmailDelivery.objects.get(message_type="loyalty_welcome")
    assert send_queued_email(delivery.pk).sent
    caption = whatsapp_notices.lifecycle_caption(
        GuestEvent.objects.get(member=member, kind=GuestEvent.Kind.JOIN), language
    )
    assert welcome.COPY[language][0] in caption and welcome.COPY[language][1] in caption
    assert welcome.COPY[language][1] in mail.outbox[0].body
    assert welcome.image(language).startswith(b"\x89PNG")
    assert len(caption.encode("utf-16-le")) // 2 < 900


def test_owner_sample_requires_owner_signed_post_and_never_accepts_recipient_override(
    settings, monkeypatch
):
    from apps.loyalty import welcome_previews as previews

    owner = user("sample-owner")
    owner.is_staff = owner.is_superuser = True
    owner.save()
    client = Client()
    client.force_login(owner)
    monkeypatch.setattr(previews, "delivery_ready", lambda: True)
    monkeypatch.setattr(
        previews, "owner_preview_binding", lambda: (owner, SimpleNamespace(phone="+12025550100"))
    )
    send = Mock(return_value=[(GuestEvent.Kind.JOIN, "accepted")])
    monkeypatch.setattr(previews, "send_catalogue", send)
    destination = reverse("notifications:loyalty_welcome_preview")
    page = client.get(destination)
    assert page.status_code == 200 and not send.called
    proof = page.context["confirmation"]
    client.post(destination, {"confirmation": "bad"})
    assert not send.called
    csrf_client = Client(enforce_csrf_checks=True)
    csrf_client.force_login(owner)
    assert csrf_client.post(destination, {"confirmation": proof}).status_code == 403
    client.post(destination, {"confirmation": proof, "phone": "+12025559999"})
    assert send.call_args.kwargs["kind"] == "welcome" and "phone" not in send.call_args.kwargs
    assert not Member.objects.exists() and not Ledger.objects.exists()
    client.force_login(user("ordinary"))
    assert client.get(destination).status_code == 403
    assert client.post(destination, {"confirmation": proof}).status_code == 403
    send.assert_called_once()
