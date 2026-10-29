"""Offline owner-only copy review; no guest message or membership changes."""

import hashlib
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from django.core.exceptions import ValidationError
from django.test import Client
from django.urls import reverse

from apps.accounts.models import VerifiedPhone
from apps.loyalty import copy_review_previews as previews
from apps.loyalty import message_previews
from apps.loyalty.models import GuestEvent, Ledger, Member
from apps.loyalty.welcome import COPY
from tests import test_loyalty_booking_invitations as existing
from tests.test_loyalty_stage8 import join, user

rig = existing.rig
pytestmark = pytest.mark.django_db


def test_review_packets_are_synthetic_and_do_not_rewrite_live_copy(settings):
    settings.SITE_BASE_URL = "https://example.invalid"
    packets = previews.packets()
    assert len(packets) == 2
    welcome, invitation = packets
    assert "خالد العتيبي" in welcome[1] and "LSA-DEMO-001" in welcome[1]
    assert "نورة الحربي" in invitation[1] and "٣٪" in invitation[1] and "٥٪" in invitation[1]
    assert all("مساعدتك الشخصية الذكية" in caption for _, caption, _ in packets)
    assert all(content.startswith(b"\x89PNG") for _, _, content in packets)
    assert all(len(caption.encode("utf-16-le")) // 2 <= 1024 for _, caption, _ in packets)
    assert not Member.objects.exists() and not Ledger.objects.exists()
    assert "بموافقتك على شروط البرنامج" in COPY["ar"][1]


@pytest.mark.parametrize("language", ["en", "fr", "invalid"])
def test_unreviewed_languages_cannot_be_sent(language):
    with pytest.raises(ValidationError):
        previews.packets(language)


def test_only_signed_owner_post_sends_two_to_the_pinned_destination(rig, monkeypatch):
    client, owner, _, _ = rig
    destination = reverse("notifications:loyalty_copy_review_preview")
    monkeypatch.setattr(
        previews, "owner_preview_binding", lambda: (owner, SimpleNamespace(phone="+12025550100"))
    )
    send = Mock(
        return_value=[("review-welcome-v2", "accepted"), ("review-invitation-v2", "accepted")]
    )
    monkeypatch.setattr(previews, "send_catalogue", send)
    page = client.get(destination)
    assert page.status_code == 200 and not send.called
    assert len(page.context["samples"]) == 2
    proof = page.context["confirmation"]
    client.post(destination, {"confirmation": "tampered"})
    assert not send.called
    csrf_client = Client(enforce_csrf_checks=True)
    csrf_client.force_login(owner)
    assert csrf_client.post(destination, {"confirmation": proof}).status_code == 403
    client.post(
        destination, {"confirmation": proof, "phone": "+12025559999", "caption": "override"}
    )
    assert send.call_args.kwargs["kind"] == "copy-review-v2"
    assert "phone" not in send.call_args.kwargs and "caption" not in send.call_args.kwargs
    assert not Member.objects.exists() and not Ledger.objects.exists()
    client.force_login(user("ordinary-reviewer"))
    assert client.get(destination).status_code == 403
    assert client.post(destination, {"confirmation": proof}).status_code == 403
    send.assert_called_once()


@pytest.mark.parametrize("uncertain", [False, True])
def test_pair_is_once_only_even_after_unknown_outcome(rig, monkeypatch, uncertain):
    _, _, provider, _ = rig
    account = user("pinned-copy-owner")
    member = join(account)
    member.number = "LSA-M000001"
    member.save(update_fields=["number"])
    binding = VerifiedPhone.objects.create(user=account, phone="+12025550100")
    monkeypatch.setattr(
        message_previews,
        "OWNER_PREVIEW_PHONE_SHA256",
        hashlib.sha256(binding.phone.encode()).hexdigest(),
    )
    monkeypatch.setattr(message_previews, "UltraMsgClient", lambda: provider)
    if uncertain:
        provider.send_image.side_effect = [{"id": "synthetic-first"}, {}]
    before = (Member.objects.count(), Ledger.objects.count(), GuestEvent.objects.count())
    first = message_previews.send_catalogue(batch="copy-review-test", kind="copy-review-v2")
    assert len(first) == 2 and first[0][1] == "accepted"
    assert first[1][1] == ("uncertain" if uncertain else "accepted")
    second = message_previews.send_catalogue(batch="copy-review-test", kind="copy-review-v2")
    assert {state for _, state in second} == {"already_attempted"}
    assert provider.send_image.call_count == 2
    assert all(
        call.kwargs["recipient"] == binding.phone for call in provider.send_image.call_args_list
    )
    assert all(
        call.kwargs["reference_id"].startswith("owner-preview:")
        for call in provider.send_image.call_args_list
    )
    assert member.number == "LSA-M000001"
    assert before == (Member.objects.count(), Ledger.objects.count(), GuestEvent.objects.count())
    binding.phone = "+12025559999"
    binding.save(update_fields=["phone"])
    with pytest.raises(ValidationError):
        message_previews.send_catalogue(batch="wrong-owner-phone", kind="copy-review-v2")
