"""Private synthetic gifts, recorded queue stubs, no real sends or provider credentials."""

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest
from django.core.cache import cache
from django.core.exceptions import PermissionDenied, ValidationError
from django.test import Client
from django.urls import reverse
from django.utils import timezone, translation

from apps.accounts.models import VerifiedPhone, profile_for
from apps.loyalty.models import Ledger, Member, Referral
from apps.loyalty.phone_gifts import (
    deliver_invitation,
    invitation_message,
    invitation_status,
    queue_invitation,
    recipient_for_phone,
)
from apps.loyalty.services import balance, reconcile, transfer
from tests.test_loyalty_stage8 import booking, join, user

pytestmark = pytest.mark.django_db
PHONE = "+12025550123"
UNKNOWN = "+12025550124"


@pytest.fixture(autouse=True)
def programme(settings, monkeypatch):
    settings.LOYALTY_ENABLED = settings.LOYALTY_UI_ENABLED = True
    settings.LOYALTY_MESSAGES_ENABLED = (
        False  # User-initiated invitation is not an automatic campaign.
    )
    settings.LOYALTY_UI_PREVIEW = False
    settings.LOYALTY_TERMS_APPROVED = True
    settings.LOYALTY_TERMS_VERSION = "synthetic-gift-v1"
    settings.LOYALTY_TERMS_REFERENCE = "/synthetic-terms/"
    settings.LOYALTY_LAUNCH_AT = "2026-10-01T00:00:00+03:00"
    settings.ULTRAMSG_ENABLED = True
    settings.ULTRAMSG_INSTANCE_ID = "fixture-only"
    settings.ULTRAMSG_TOKEN = "fixture-only-not-a-real-secret"
    settings.SITE_BASE_URL = "https://luxurysmartapartments.com"
    clock = [datetime(2026, 10, 10, 13, tzinfo=UTC)]
    monkeypatch.setattr(timezone, "now", lambda: clock[0])
    cache.clear()
    return clock


def setup_pair(*, receiver_member=True, verified=True):
    a, b = user("phone-gift-sender"), user("phone-gift-recipient")
    am = join(a)
    am.first_completed_at = timezone.now() - timedelta(days=3)
    am.invitation_token = "synthetic-gift-sender-token"
    am.save(update_fields=["first_completed_at", "invitation_token"])
    bm = join(b) if receiver_member else None
    if verified:
        VerifiedPhone.objects.create(user=b, phone=PHONE)
    r = booking(a)
    reconcile(r.pk)
    c = Client()
    c.force_login(a)
    return c, am, bm, b


def review(c, phone=PHONE):
    return c.post(
        reverse("accounts:rewards"),
        {
            "action": "gift_phone_review",
            "phone": phone,
            "phone_country": "US",
            "amount": "10.00",
        },
    )


def queue(sender, *, phone=UNKNOWN, key=None, language="ar", consent=True):
    return queue_invitation(
        sender.customer,
        phone=phone,
        language=language,
        key=key or "gift-invite:" + str(uuid.uuid4()),
        consent=consent,
    )


def test_default_phone_form_shared_input_and_membership_fallback_unique_ids():
    c, am, _, _ = setup_pair()
    response = c.get(reverse("accounts:rewards"))
    html = response.content.decode()
    assert "data-checkout-phone" in html and "checkout-phone.js" in html
    assert "gift_phone_review" in html and "transfer_review" in html
    assert 'id="gift_amount"' in html and html.count('id="id_amount"') == 1
    assert balance(am) == Decimal("22.50")


@pytest.mark.parametrize("phone", [PHONE, "0012025550123", "+1 (202) 555-0123", "٠٠١٢٠٢٥٥٥٠١٢٣"])
def test_phone_review_masks_name_and_keeps_ledger_untouched_then_confirms_once(phone):
    c, am, bm, _ = setup_pair()
    original = list(Referral.objects.values())
    response = review(c, phone)
    confirmation = response.context["loyalty_confirmation"]
    assert confirmation["phone"] == "••••0123" and confirmation["name"].endswith("…")
    assert bm.customer.get_full_name() not in response.content.decode()
    assert balance(am) == Decimal("22.50") and balance(bm) == 0
    data = {"action": "transfer_confirm", "confirmation": confirmation["token"]}
    assert c.post(reverse("accounts:rewards"), data).status_code == 302
    assert c.post(reverse("accounts:rewards"), data).status_code == 302
    assert balance(am) == Decimal("12.50") and balance(bm) == Decimal("10.00")
    assert Ledger.objects.filter(event_type="transfer").count() == 2
    assert list(Referral.objects.values()) == original


def test_editable_contact_is_not_identity_and_nonmember_gets_invite_not_points():
    c, am, bm, b = setup_pair(verified=False)
    p = profile_for(b)
    p.phone = PHONE
    p.save(update_fields=["phone"])
    response = review(c)
    assert response.context["loyalty_confirmation"] is None
    assert response.context["loyalty_gift_invitation"]["eligible"]
    assert balance(am) == Decimal("22.50") and balance(bm) == 0
    assert Ledger.objects.filter(event_type="transfer").count() == 0
    assert not recipient_for_phone(PHONE)[0]


def test_changed_verified_binding_blocks_old_confirmation():
    c, am, bm, b = setup_pair()
    token = review(c).context["loyalty_confirmation"]["token"]
    VerifiedPhone.objects.filter(user=b).update(version=uuid.uuid4())
    response = c.post(
        reverse("accounts:rewards"), {"action": "transfer_confirm", "confirmation": token}
    )
    assert response.status_code == 200
    assert balance(am) == Decimal("22.50") and balance(bm) == 0


def test_member_that_withdrew_or_was_disabled_cannot_receive_after_review():
    c, am, bm, b = setup_pair()
    token = review(c).context["loyalty_confirmation"]["token"]
    b.is_active = False
    b.save(update_fields=["is_active"])
    assert (
        c.post(
            reverse("accounts:rewards"), {"action": "transfer_confirm", "confirmation": token}
        ).status_code
        == 200
    )
    with pytest.raises(ValidationError):
        transfer(am.customer, "synthetic-inactive", bm.number, "10.00")
    assert balance(bm) == 0


def test_invitation_requires_explicit_consent_and_only_queues_once(
    django_capture_on_commit_callbacks,
):
    c, am, _, _ = setup_pair(receiver_member=False)
    response = review(c)
    token = response.context["loyalty_gift_invitation"]["token"]
    original = Ledger.objects.count()
    data = {"action": "gift_invitation_confirm", "confirmation": token, "language": "fr"}
    with patch("apps.loyalty.phone_gifts.deliver_invitation.delay") as enqueue:
        assert c.post(reverse("accounts:rewards"), data).status_code == 200
        assert not enqueue.called
        data["consent"] = "on"
        with django_capture_on_commit_callbacks(execute=True):
            assert c.post(reverse("accounts:rewards"), data).status_code == 302
        with django_capture_on_commit_callbacks(execute=True):
            assert c.post(reverse("accounts:rewards"), data).status_code == 302
        assert enqueue.call_count == 1
    assert Ledger.objects.count() == original and balance(am) == Decimal("22.50")
    assert Member.objects.count() == 1  # No auto-enrolment or account creation.
    pointer = c.session["loyalty_gift_invitation"]
    assert invitation_status(am.customer, pointer)["status"] == "queued"
    assert pointer["key"].startswith("gift-invite:")


def test_queue_failure_does_not_claim_sent_or_change_points(django_capture_on_commit_callbacks):
    _, am, _, _ = setup_pair(receiver_member=False)
    with patch(
        "apps.loyalty.phone_gifts.deliver_invitation.delay",
        side_effect=RuntimeError("synthetic broker failure"),
    ):
        with django_capture_on_commit_callbacks(execute=True):
            pointer = queue(am)
    assert invitation_status(am.customer, pointer)["status"] == "queue_failed"
    assert balance(am) == Decimal("22.50")


@pytest.mark.parametrize(
    "language,title", [("ar", "دعوة إلى مكافآت"), ("en", "An invitation"), ("fr", "Une invitation")]
)
def test_one_language_business_invitation_send_once_with_sender_link(language, title):
    _, am, _, _ = setup_pair(receiver_member=False)
    pointer = queue(am, language=language)
    provider = MagicMock()
    provider.__enter__.return_value = provider
    with patch("apps.loyalty.phone_gifts.UltraMsgClient", return_value=provider):
        deliver_invitation(am.pk, pointer["key"])
        deliver_invitation(am.pk, pointer["key"])
    assert provider.send_text.call_count == 1
    body = provider.send_text.call_args.kwargs["body"]
    assert (
        title in body
        and "/i/" in body
        and provider.send_text.call_args.kwargs["recipient"] == UNKNOWN
    )
    other_titles = {"دعوة إلى مكافآت", "An invitation", "Une invitation"} - {title}
    assert not any(t in body for t in other_titles)
    assert invitation_status(am.customer, pointer)["status"] == "accepted"
    assert balance(am) == Decimal("22.50")


def test_unknown_provider_outcome_never_resends():
    _, am, _, _ = setup_pair(receiver_member=False)
    pointer = queue(am)
    provider = MagicMock()
    provider.__enter__.return_value = provider
    provider.send_text.side_effect = RuntimeError("synthetic timeout with private request details")
    with patch("apps.loyalty.phone_gifts.UltraMsgClient", return_value=provider):
        deliver_invitation(am.pk, pointer["key"])
        deliver_invitation(am.pk, pointer["key"])
    assert provider.send_text.call_count == 1
    assert invitation_status(am.customer, pointer)["status"] == "uncertain"


def test_member_joining_before_worker_skips_invite_and_never_transfers():
    _, am, _, b = setup_pair(receiver_member=False)
    pointer = queue(am, phone=PHONE)
    bm = join(b)
    with patch("apps.loyalty.phone_gifts.UltraMsgClient") as provider:
        deliver_invitation(am.pk, pointer["key"])
    assert not provider.called
    assert invitation_status(am.customer, pointer)["status"] == "recipient_ready"
    assert balance(bm) == 0 and balance(am) == Decimal("22.50")


def test_caps_qualification_pilot_and_disabled_transport_fail_closed(settings):
    _, am, _, _ = setup_pair(receiver_member=False)
    with pytest.raises(PermissionDenied):
        queue(am, consent=False)
    queue(am)
    with pytest.raises(ValidationError):
        queue(am)  # Same target, different request: daily target cap.
    am.first_completed_at = None
    am.save(update_fields=["first_completed_at"])
    with pytest.raises(ValidationError):
        queue(am, phone=PHONE)
    settings.LOYALTY_PILOT_ONLY = True
    settings.LOYALTY_PILOT_EMAILS = ["someone-else@example.invalid"]
    with pytest.raises(PermissionDenied):
        queue(am, phone=PHONE)
    settings.LOYALTY_PILOT_ONLY = False
    settings.ULTRAMSG_ENABLED = False
    with pytest.raises(ValidationError):
        queue(am, phone=PHONE)


def test_message_never_adds_guest_controlled_links_or_three_languages():
    _, am, _, _ = setup_pair(receiver_member=False)
    am.customer.first_name = "*Name* https://evil.invalid\nhttps://other.invalid"
    body = invitation_message(
        am, language="ar", url="https://luxurysmartapartments.com/i/synthetic/"
    )
    assert body.count("https://") == 1 and "*Name*" not in body
    with translation.override("fr"):
        response = Client().get(reverse("accounts:rewards"))
        assert response.status_code == 302  # Login still required.


def test_expired_queue_and_session_status_do_not_leak_other_accounts(programme):
    _, am, _, _ = setup_pair(receiver_member=False)
    pointer = queue(am)
    programme[0] += timedelta(minutes=16)
    with patch("apps.loyalty.phone_gifts.UltraMsgClient") as provider:
        deliver_invitation(am.pk, pointer["key"])
    assert not provider.called
    assert invitation_status(am.customer, pointer)["status"] == "disabled"
    other = user("unrelated")
    assert invitation_status(other, pointer) is None


def test_worker_discovers_the_explicit_task():
    from config.celery import app

    app.autodiscover_tasks(force=True)
    assert "apps.loyalty.phone_gifts.deliver_invitation" in app.tasks


def test_invitation_then_voluntary_join_and_phone_verification_then_manual_gift():
    c, am, _, b = setup_pair(receiver_member=False, verified=False)
    pointer = queue(am, phone=PHONE)
    with patch("apps.loyalty.phone_gifts.UltraMsgClient") as provider:
        provider.return_value.__enter__.return_value.send_text.return_value = {
            "id": "fixture-accepted"
        }
        deliver_invitation(am.pk, pointer["key"])
    assert Member.objects.count() == 1 and balance(am) == Decimal("22.50")
    bm = join(b, invitation_token=am.invitation_token, confirmed_inviter=am.pk)
    assert balance(bm) == 0 and balance(am) == Decimal("22.50")
    VerifiedPhone.objects.create(user=b, phone=PHONE)
    confirmation = review(c).context["loyalty_confirmation"]
    assert (
        c.post(
            reverse("accounts:rewards"),
            {"action": "transfer_confirm", "confirmation": confirmation["token"]},
        ).status_code
        == 302
    )
    assert balance(bm) == Decimal("10.00") and balance(am) == Decimal("12.50")
    assert bm.referral.inviter_id == am.pk


def test_saved_recipient_language_wins_and_other_sender_cannot_use_confirmation():
    c, am, _, b = setup_pair(receiver_member=False)
    p = profile_for(b)
    p.preferred_language = "fr"
    p.save(update_fields=["preferred_language"])
    response = review(c)
    assert response.context["loyalty_gift_invitation_form"].fields["language"].disabled
    pointer = queue(am, phone=PHONE, language="ar")
    provider = MagicMock()
    provider.__enter__.return_value = provider
    with patch("apps.loyalty.phone_gifts.UltraMsgClient", return_value=provider):
        deliver_invitation(am.pk, pointer["key"])
    assert "Une invitation" in provider.send_text.call_args.kwargs["body"]
    other = user("different-sender")
    join(other)
    client = Client()
    client.force_login(other)
    assert (
        client.post(
            reverse("accounts:rewards"),
            {
                "action": "gift_invitation_confirm",
                "confirmation": response.context["loyalty_gift_invitation"]["token"],
                "language": "ar",
                "consent": "on",
            },
        ).status_code
        == 403
    )
