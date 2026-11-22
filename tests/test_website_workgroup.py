"""Real source models and synthetic bridge proofs; no real sends/bookings/charges."""

import uuid
from datetime import timedelta
from unittest.mock import Mock

import pytest
from django.utils import timezone

from apps.loyalty import booking_invitations as invitations
from apps.loyalty.models import JoinInvitation
from apps.payments.models import PaymentRequestDelivery
from apps.payments.payment_link_tasks import deliver_payment_request_task
from apps.payments.payment_links import issue_request
from apps.reservations import telegram_authority as authority
from apps.reservations import telegram_website_workgroup as service
from apps.reservations import telegram_workgroup as group
from apps.reservations.telegram_stay_models import TelegramWebsiteHandoff
from tests import test_loyalty_booking_invitations as loyalty
from tests import test_reservation_payment_links as payments

pytestmark = pytest.mark.django_db
rig = loyalty.rig
isolated_settings = payments.isolated_settings


@pytest.fixture
def handoff_settings(settings):
    settings.TELEGRAM_WORKGROUP_HANDOFF_ENABLED = True
    settings.TELEGRAM_WORKGROUP_CUTOVER_AT = (timezone.now() - timedelta(days=1)).isoformat()
    settings.HYPERPAY_ENVIRONMENT = "production"


def proof(row, **changes):
    return {
        "contract": "tg39_workgroup_handoff_v1",
        "available": True,
        "channel": "workgroup_manual",
        "workgroup_id": group.WORKGROUP_ID,
        "workgroup_binding_fingerprint": group.fingerprint(),
        "guest_accepted": False,
        "delivered": False,
        "read": False,
        **service.metadata(row),
        "attempt_id": str(uuid.uuid4()),
        "status": "handed_off",
        "api_accepted": True,
        "provider_message_id": "102",
        "provider_message_ids": ["101", "102"],
        "accepted_at": timezone.now().isoformat(),
        "recorded_at": None,
        **changes,
    }


@pytest.fixture
def bridge(monkeypatch):
    calls = []

    class Backend:
        authority_context = None

        def call(self, action, key):
            row = TelegramWebsiteHandoff.objects.get(pk=self.authority_context["job"])
            allowed = service.authorize(self.authority_context, action, key, {})
            assert allowed["dispatch_mode"] == "workgroup_manual"
            assert allowed["workgroup_id"] == group.WORKGROUP_ID
            if action == "website_handoff":
                assert allowed["packet"]["asset_base64"]
                assert allowed["packet"]["caption_html"]
            calls.append(action)
            return proof(row)

        def close(self):
            pass

    monkeypatch.setattr(service, "Backend", Backend)
    return calls


@pytest.fixture
def delivery(isolated_settings, handoff_settings):
    stay = payments.manual_stay()
    stay.is_test = False
    stay.save(update_fields=["is_test"])
    request = issue_request(stay.pk)
    return PaymentRequestDelivery.objects.create(request=request, kind="link")


def test_standalone_payment_copy_preserves_logo_and_does_not_mark_guest_sent(delivery, bridge):
    result = deliver_payment_request_task(delivery.pk)
    assert result["status"] == "handed_off" and not result["guest_accepted"]
    delivery.refresh_from_db()
    assert delivery.status == "handed_off" and delivery.sent_at is None
    assert delivery.provider_message_id == ""
    row = TelegramWebsiteHandoff.objects.get()
    assert row.state == "handed_off" and row.provider_message_ids == ["101", "102"]
    assert set(bridge) == {"website_handoff"}
    assert deliver_payment_request_task(delivery.pk)["status"] == "handed_off"
    assert len(bridge) == 1
    saved = str(list(TelegramWebsiteHandoff.objects.values()))
    assert (
        "asset_base64" not in saved and "caption_html" not in saved and "guest_phone" not in saved
    )
    assert delivery.request.status == "open" and not delivery.request.attempts.exists()


def test_source_rechecks_financial_fingerprint_before_authority(delivery, monkeypatch):
    source = service.source_copy("payment_request", str(delivery.pk))
    called = Mock()
    monkeypatch.setattr(service, "Backend", called)
    reservation = delivery.request.reservation
    reservation.total_price += 1
    reservation.save(update_fields=["total_price"])
    result = deliver_payment_request_task(delivery.pk)
    assert result["status"] == "held" and not called.called
    assert not TelegramWebsiteHandoff.objects.exists()
    assert source["purpose"] == "payment_link"


def test_unknown_then_status_only_import_after_revocation_no_provider_repost(delivery, monkeypatch):
    class Backend:
        authority_context = None

        def call(self, action, key):
            assert action == "website_handoff"
            raise service.Unavailable

        def close(self):
            pass

    monkeypatch.setattr(service, "Backend", Backend)
    assert deliver_payment_request_task(delivery.pk)["status"] == "uncertain"
    row = TelegramWebsiteHandoff.objects.get()
    request = delivery.request
    request.status = "revoked"
    request.save(update_fields=["status"])
    # Historical status scope never reauthorizes the financial resource.
    packet = service.authorize({"job": str(row.pk)}, "website_handoff_status", str(row.pk), {})[
        "packet"
    ]
    assert "asset_base64" not in packet and "caption_html" not in packet
    assert service.import_proof(row, proof(row))
    delivery.refresh_from_db()
    request.refresh_from_db()
    assert delivery.status == "handed_off" and request.status == "revoked"


@pytest.mark.parametrize(
    "change",
    [
        {"guest_accepted": True},
        {"delivered": True},
        {"read": True},
        {"workgroup_id": "-999"},
        {"status": "accepted"},
        {"source_revision": "a" * 64},
        {"provider_message_ids": []},
        {"provider_message_ids": ["101"]},
        {"content_sha256": "b" * 64},
    ],
)
def test_forged_workgroup_proofs_cannot_be_imported(delivery, monkeypatch, change):
    monkeypatch.setattr(service, "call", lambda *a: None)
    deliver_payment_request_task(delivery.pk)
    row = TelegramWebsiteHandoff.objects.get()
    with pytest.raises(authority.Denied):
        service.import_proof(row, proof(row, **change))
    row.refresh_from_db()
    assert row.state == "dispatching" and row.accepted_at is None


def test_original_loyalty_admin_approval_yields_workgroup_copy_without_guest_send(
    rig, handoff_settings, bridge
):
    client, _, provider, _ = rig
    booking = loyalty.past_booking()
    data = loyalty.payload(client, booking)
    assert client.post(loyalty.url(booking), data).status_code == 302
    booking.refresh_from_db()
    request_id = booking.loyalty_state[invitations.LATEST]
    result = invitations.deliver_booking_invitation(str(booking.pk), "whatsapp", request_id)
    assert result["status"] == "handed_off" and not result["guest_accepted"]
    booking.refresh_from_db()
    assert loyalty.whatsapp_row(booking)["status"] == "handed_off"
    invitation = JoinInvitation.objects.get()
    assert invitation.channel == "workgroup_manual" and invitation.state == "handed_off"
    assert not provider.send_image.called
    assert bridge == ["website_handoff"]


def test_no_historical_queue_backfill(delivery, settings, monkeypatch):
    settings.TELEGRAM_WORKGROUP_CUTOVER_AT = (timezone.now() + timedelta(minutes=1)).isoformat()
    called = Mock()
    monkeypatch.setattr(service, "Backend", called)
    assert deliver_payment_request_task(delivery.pk)["status"] == "held"
    assert not called.called and not TelegramWebsiteHandoff.objects.exists()


def test_guest_markup_is_literal_in_managers_copy():
    guest = "<script> *Guest*"
    body = service.as_html("*Welcome* " + guest, guest)
    assert body == "<b>Welcome</b> &lt;script&gt; *Guest*"
