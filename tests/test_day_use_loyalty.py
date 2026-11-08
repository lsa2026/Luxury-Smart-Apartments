"""Day-use invitation boundary: synthetic DB, receipts and providers; no points."""

import hashlib
import json
import uuid
from datetime import timedelta
from decimal import Decimal
from unittest.mock import Mock

import pytest
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.utils import timezone

from apps.loyalty import invitation_registry as registry
from apps.loyalty.models import JoinInvitation, Ledger, Member
from apps.payments.models import DayUsePaymentAttempt
from apps.reservations import day_use08_payments as payments
from apps.reservations import day_use_loyalty as service
from apps.reservations.models import (
    DayUse08DeliveryAttempt,
    DayUse08PaymentDelivery,
    DayUse08PaymentEvent,
    DayUseStay,
)
from tests.test_availability_service import make_property
from tests.test_manual_booking_operations_phase61 import owner

pytestmark = pytest.mark.django_db


@pytest.fixture
def rig(settings, monkeypatch):
    settings.DAY_USE08_ENABLED = False
    settings.SITE_BASE_URL = "https://testserver"
    actor = owner()
    prop = make_property()
    prop.hostaway_listing_map_id = 97008
    prop.save()
    start = timezone.now() + timedelta(days=2)
    paid = timezone.now()
    stay = DayUseStay.objects.create(
        request_id=uuid.uuid4(),
        property=prop,
        starts_at=start,
        ends_at=start + timedelta(hours=11),
        inventory_arrival_date=start.date() - timedelta(days=1),
        inventory_departure_date=start.date() + timedelta(days=1),
        guest_name="Synthetic Day Guest",
        guest_phone="+16502530000",
        guest_email="day-use@example.test",
        guest_language="ar",
        guests=1,
        amount_sar=Decimal("740.93"),
        environment="production",
        status="paid",
        expires_at=start,
        availability_checked_at=paid,
        created_by=actor,
        paid_at=paid,
        communication_revision=1,
        availability_evidence={
            "creation_mode": "hostaway_first_v2",
            "hostaway_day_use": {"state": "confirmed", "reservation_id": 990008},
        },
    )
    attempt = DayUsePaymentAttempt.objects.create(
        stay=stay,
        merchant_transaction_id="synthetic-day-use",
        amount=stay.amount_sar,
        currency="SAR",
        status="succeeded",
        verified_at=paid,
        provider_payment_id="synthetic-payment-day-use",
    )
    settings.DAY_USE08_ENABLED = True
    facts = payments.verified_facts(stay)
    assert facts is not None
    event = DayUse08PaymentEvent.objects.create(
        stay=stay,
        payment_revision=1,
        proof_key=facts["proof_key"],
        proof_kind=facts["proof_kind"],
        payload=facts,
    )
    notice = DayUse08PaymentDelivery.objects.create(
        reference_id="synthetic-day-payment-success",
        event=event,
        message_type="payment_success",
        state="accepted",
        provider_id="synthetic-success",
        provider_reference="synthetic-success-ref",
    )
    delivery = DayUse08PaymentDelivery.objects.create(
        reference_id="synthetic-day-invitation",
        event=event,
        message_type="loyalty_invitation",
        state="posting",
        provider_reference="",
    )
    phone = "".join(c for c in stay.guest_phone if c.isdigit())
    stamp = int(start.timestamp() * 1000)
    guide = f"guide:{prop.hostaway_listing_map_id}:{stamp}:{phone}"
    for suffix in ("r1:status", "rules:" + phone, guide):
        reference = (
            "lsa08-"
            + hashlib.md5(f"lsa08:{stay.pk}:{suffix}".encode(), usedforsecurity=False).hexdigest()
        )
        DayUse08DeliveryAttempt.objects.create(
            reference_id=reference,
            stay=stay,
            revision=1,
            payload_hash="a" * 64,
            state="accepted",
            provider_id="synthetic-" + suffix[:25],
        )
    acks = {"core": 2, "payment": 2}
    monkeypatch.setattr(service, "exact_receipt_messages", lambda item: [{"ack": acks["core"]}])
    monkeypatch.setattr(payments, "receipts", lambda item: [{"ack": acks["payment"]}])
    monkeypatch.setattr(service, "ready", lambda: True)
    provider = Mock()
    provider.send_image.return_value = {"id": "synthetic-invite"}
    context = Mock()
    context.__enter__ = Mock(return_value=provider)
    context.__exit__ = Mock(return_value=False)
    monkeypatch.setattr(service, "UltraMsgClient", lambda: context)

    def send(**overrides):
        kwargs = {"payment_event_id": event.pk, "payment_delivery_id": delivery.pk}
        kwargs.update(overrides)
        return json.loads(service.invite_paid_booking(stay, 1, **kwargs).content)

    return type(
        "Rig",
        (),
        {
            "stay": stay,
            "event": event,
            "delivery": delivery,
            "notice": notice,
            "attempt": attempt,
            "provider": provider,
            "acks": acks,
            "send": staticmethod(send),
        },
    )()


def claim(stay, **overrides):
    kwargs = dict(
        key=registry.day_use_booking_key(stay),
        phone=stay.guest_phone,
        email=stay.guest_email,
        customer_id=None,
        language=stay.guest_language,
        event_id="day-use:" + str(stay.pk),
        source="day_use08",
        origin="website",
        channel="whatsapp",
    )
    kwargs.update(overrides)
    result, row = registry.claim(**kwargs)
    assert result == "eligible"
    return row


def test_paid_future_stay_invites_once_without_membership_or_points(rig):
    before = (Member.objects.count(), Ledger.objects.count())
    assert rig.stay.starts_at > timezone.now()
    assert rig.send()["code"] == "accepted"
    assert rig.send()["code"] == "duplicate"
    row = JoinInvitation.objects.get()
    assert row.reservation_key == "hostaway:990008"
    assert row.source == "day_use08"
    assert row.provider_id == "synthetic-invite"
    rig.delivery.refresh_from_db()
    assert rig.delivery.provider_reference == row.provider_reference
    assert rig.provider.send_image.call_count == 1
    assert before == (Member.objects.count(), Ledger.objects.count())


@pytest.mark.parametrize("gate", ["core", "payment"])
@pytest.mark.parametrize("ack", ["server", 1, 0])
def test_provider_acceptance_without_delivery_cannot_invite(rig, gate, ack):
    rig.acks[gate] = ack
    assert rig.send()["code"] in {"core_pending", "payment_notice_pending"}
    rig.provider.send_image.assert_not_called()
    assert not JoinInvitation.objects.exists()


def test_missing_financial_event_cannot_use_legacy_sender(rig):
    assert rig.send(payment_event_id=None)["code"] == "managed_by_day_use08"
    assert rig.send(payment_delivery_id=None)["code"] == "managed_by_day_use08"
    rig.provider.send_image.assert_not_called()


@pytest.mark.parametrize("change", ["expired", "cancelled", "pending", "changed_amount"])
def test_invalid_current_payment_or_window_blocks_invitation(rig, change):
    if change == "expired":
        # Preserve the positive-window constraint; mutate the original source in DB.
        DayUseStay.objects.filter(pk=rig.stay.pk).update(
            starts_at=timezone.now() - timedelta(hours=12),
            ends_at=timezone.now() - timedelta(hours=1),
        )
    elif change == "cancelled":
        DayUseStay.objects.filter(pk=rig.stay.pk).update(status="cancelled")
    elif change == "changed_amount":
        DayUseStay.objects.filter(pk=rig.stay.pk).update(amount_sar=Decimal("741.00"))
    else:
        DayUsePaymentAttempt.objects.filter(pk=rig.attempt.pk).update(status="review")
    rig.stay.refresh_from_db()
    assert rig.send()["code"] in {"not_eligible", "payment_notice_pending"}
    rig.provider.send_image.assert_not_called()
    assert not JoinInvitation.objects.exists()


def test_foreign_payment_event_cannot_unlock_invitation(rig):
    assert rig.send(payment_event_id=uuid.uuid4())["code"] == "payment_notice_pending"
    assert not JoinInvitation.objects.exists()


def test_wrong_delivery_reference_cannot_consume_claim(rig):
    assert rig.send(payment_delivery_id="nonexistent")["code"] == "stale"
    assert not JoinInvitation.objects.exists()
    rig.provider.send_image.assert_not_called()


def test_same_hostaway_identity_suppresses_other_source_even_after_contact_change(rig):
    with registry.invitation_lock():
        row = claim(
            rig.stay,
            source="external-platform",
            origin="n8n",
            phone="+442079460001",
            email="other@example.test",
        )
    assert row.reservation_key == registry.canonical_booking("990008")[0]
    assert rig.send()["code"] == "duplicate"
    rig.provider.send_image.assert_not_called()


def test_cross_booking_contact_suppresses_day_use_invitation(rig):
    with registry.invitation_lock():
        decision, _ = registry.claim(
            key="hostaway:990009",
            phone=rig.stay.guest_phone,
            email="other@example.test",
            customer_id=None,
            language="en",
            event_id="synthetic-other",
            source="morning07",
            origin="website",
            channel="whatsapp",
        )
    assert decision == "eligible"
    assert rig.send()["code"] == "duplicate"


@pytest.mark.parametrize("status", ["active", "withdrawn"])
def test_existing_member_email_suppresses_without_reactivating(rig, status):
    guest = get_user_model().objects.create_user(
        username="synthetic-day-member",
        email=rig.stay.guest_email.upper(),
    )
    member = Member.objects.create(
        customer=guest,
        number="DU-synthetic-member",
        status=status,
        consent_at=timezone.now(),
        terms_version="synthetic-consent",
        terms_reference="https://testserver/terms",
        language="ar",
    )
    assert rig.send()["code"] == "member"
    rig.provider.send_image.assert_not_called()
    member.refresh_from_db()
    assert member.status == status
    assert not Ledger.objects.exists() and not JoinInvitation.objects.exists()


def test_legacy_queue_hook_never_schedules_an_alternate_sender(rig):
    assert service.queue_paid_invitation(rig.stay) is None
    rig.provider.send_image.assert_not_called()


def test_registry_requires_explicit_day_use_source_and_payment_event(rig):
    with registry.invitation_lock():
        row = claim(rig.stay)
        with pytest.raises(ValidationError):
            registry.consume(row, phone=row.phone)
        with pytest.raises(ValidationError):
            registry.consume(row, phone=row.phone, day_use_stay=rig.stay)
    row.refresh_from_db()
    assert row.state == "claimed"
    assert row.dispatched_at is None


def test_registry_refuses_non_day_use_instance_and_wrong_source(rig):
    with pytest.raises(ValidationError):
        registry.day_use_booking_key(object())
    with registry.invitation_lock():
        row = claim(rig.stay, source="morning07")
        with pytest.raises(ValidationError):
            registry.consume(
                row,
                phone=row.phone,
                day_use_stay=rig.stay,
                day_use_payment_event_id=rig.event.pk,
            )
    row.refresh_from_db()
    assert row.state == "claimed"


def test_registry_reloads_contact_instead_of_trusting_supplied_instance(rig):
    with registry.invitation_lock():
        row = claim(rig.stay)
        DayUseStay.objects.filter(pk=rig.stay.pk).update(guest_phone="+442079460001")
        with pytest.raises(ValidationError):
            registry.consume(
                row,
                phone=row.phone,
                day_use_stay=rig.stay,
                day_use_payment_event_id=rig.event.pk,
            )
    row.refresh_from_db()
    assert row.dispatched_at is None


def test_registry_rechecks_core_delivery_at_consumption(rig):
    with registry.invitation_lock():
        row = claim(rig.stay)
        rig.acks["core"] = 1
        with pytest.raises(ValidationError):
            registry.consume(
                row,
                phone=row.phone,
                day_use_stay=rig.stay,
                day_use_payment_event_id=rig.event.pk,
            )
    row.refresh_from_db()
    assert row.state == "claimed"


def test_window_ending_during_receipt_lookup_cannot_issue_send_permit(rig, monkeypatch):
    def received_at_expiry(*args, **kwargs):
        monkeypatch.setattr(timezone, "now", lambda: rig.stay.ends_at)
        return True

    with registry.invitation_lock():
        row = claim(rig.stay)
        monkeypatch.setattr(service, "core_delivered", received_at_expiry)
        with pytest.raises(ValidationError):
            registry.consume(
                row,
                phone=row.phone,
                day_use_stay=rig.stay,
                day_use_payment_event_id=rig.event.pk,
            )
    row.refresh_from_db()
    assert row.state == "claimed" and row.dispatched_at is None


def test_unknown_provider_result_is_not_reposted(rig):
    rig.provider.send_image.side_effect = RuntimeError("synthetic uncertain result")
    assert rig.send()["code"] == "unknown"
    assert rig.send()["code"] == "duplicate"
    assert rig.provider.send_image.call_count == 1
    assert JoinInvitation.objects.get().state == "unknown"


@pytest.mark.parametrize("language", ["ar", "en", "fr"])
def test_optional_copy_has_no_current_daytime_reward_promise(rig, language):
    rig.stay.guest_language = language
    text = service.caption(rig.stay)
    assert len(text.encode("utf-16-le")) // 2 <= 1024
    assert "/my-bookings/rewards/" in text
    assert "740.93" not in text and "dayuse08" not in text
    assert {
        "ar": "لا تتضمن هذه الدعوة وعدًا بنقاط عن الإقامة النهارية الحالية",
        "en": "does not promise points for the current daytime stay",
        "fr": "ne promet aucun point pour le séjour de jour actuel",
    }[language] in text
