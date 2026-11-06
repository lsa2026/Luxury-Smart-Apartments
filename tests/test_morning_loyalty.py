"""Paid-only optional invitations. Synthetic providers and contact identities only."""

from datetime import timedelta
from unittest.mock import patch

import pytest
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from apps.loyalty.models import JoinInvitation, Ledger, Member
from apps.reservations import morning_loyalty as service
from apps.reservations.models import MorningStay
from apps.reservations.morning07_communications import delivery_token
from tests.test_morning07_communications import enabled as enabled
from tests.test_morning07_communications import paid_stay
from tests.test_morning_stays import create

pytestmark = pytest.mark.django_db


def post(stay, **changes):
    return Client(enforce_csrf_checks=True).post(
        reverse("integrations:morning07_loyalty"),
        {"core_delivered": True} | changes,
        content_type="application/json",
        HTTP_X_LSA_MORNING07_TOKEN=delivery_token(stay),
    )


@pytest.fixture
def rig(request):
    request.getfixturevalue("enabled")
    with (
        patch.object(service, "ready", return_value=True),
        patch.object(service, "core_delivered", return_value=True),
        patch.object(service, "UltraMsgClient") as client,
    ):
        provider = client.return_value.__enter__.return_value
        provider.send_image.return_value = {"id": "synthetic-invitation", "sent": "true"}
        yield provider


def test_paid_invitation_uses_brand_image_and_shared_registry_once(rig):
    stay = paid_stay()
    assert post(stay).json()["code"] == "accepted"
    assert post(stay).json()["code"] == "duplicate"
    row = JoinInvitation.objects.get()
    assert row.reservation_key == "morning07:" + str(stay.pk)
    assert row.state == "accepted" and rig.send_image.call_count == 1
    values = rig.send_image.call_args.kwargs
    assert values["content"].startswith(b"\x89PNG")
    assert "٣٪" in values["caption"] and "٥٪" in values["caption"]
    assert "سما" in values["caption"] and "الحجز عبر منصة خارجية" not in values["caption"]
    assert values["reference_id"] == row.provider_reference
    assert not Member.objects.exists() and not Ledger.objects.exists()


@pytest.mark.parametrize("state", ["open", "expired", "processing", "review", "cancelled"])
def test_no_invitation_before_payment_or_on_cancelled_request(rig, state):
    stay = create()
    MorningStay.objects.filter(pk=stay.pk).update(status=state)
    assert post(stay).status_code == 404
    assert not JoinInvitation.objects.exists()
    rig.send_image.assert_not_called()


def test_delivery_or_member_or_ended_stay_suppresses_invitation(rig):
    stay = paid_stay()
    assert post(stay, core_delivered=False).json()["code"] == "core_pending"
    with patch.object(service, "core_delivered", return_value=False):
        assert post(stay).json()["code"] == "core_pending"
    with patch.object(service.registry, "member_exists", return_value=True):
        assert post(stay).json()["code"] == "member"
    MorningStay.objects.filter(pk=stay.pk).update(
        starts_at=timezone.now() - timedelta(hours=2),
        ends_at=timezone.now() - timedelta(seconds=1),
    )
    assert post(stay).json()["code"] == "not_eligible"
    rig.send_image.assert_not_called()


def test_unknown_provider_outcome_is_not_reposted(rig):
    from apps.notifications.services.ultramsg import UltraMsgConnectionError

    stay = paid_stay()
    rig.send_image.side_effect = UltraMsgConnectionError()
    assert post(stay).json()["code"] == "unknown"
    assert post(stay).json()["code"] == "duplicate"
    assert rig.send_image.call_count == 1


def test_core_delivery_checks_exact_owned_provider_receipts(request):
    request.getfixturevalue("enabled")
    import hashlib

    from apps.reservations.models import Morning07DeliveryAttempt

    stay = paid_stay()
    phone = stay.guest_phone.replace("+", "")
    stamp = int(stay.starts_at.timestamp() * 1000)
    guide = f"guide:{stay.property.hostaway_listing_map_id}:{stamp}:{phone}"
    for suffix in ("r1:status", "rules:" + phone, guide):
        ref = (
            "lsa07-"
            + hashlib.md5(f"lsa07:{stay.pk}:{suffix}".encode(), usedforsecurity=False).hexdigest()
        )
        Morning07DeliveryAttempt.objects.create(
            reference_id=ref,
            stay=stay,
            revision=1,
            payload_hash="a" * 64,
            state="accepted",
            provider_id="synthetic-" + suffix,
        )
    with patch.object(service, "exact_receipt_messages", return_value=[{"ack": "server"}]):
        assert not service.core_delivered(stay, 1)
    with patch.object(service, "exact_receipt_messages", return_value=[{"ack": 2}]):
        assert service.core_delivered(stay, 1)


@pytest.mark.parametrize("language", ["ar", "en", "fr"])
def test_morning_invitation_uses_exact_approved_art_and_optional_terms(rig, language):
    stay = paid_stay()
    stay.guest_language = language
    text = service.caption(stay)
    assert len(text.encode("utf-16-le")) // 2 <= 1024
    assert "my-bookings/rewards/" in text
