"""Exercise real owner POSTs and database locks; only external providers are stubbed.

Run this file on PostgreSQL in CI as well as SQLite. The original 500 cannot be
detected by SQLite, which silently omits SELECT FOR UPDATE.
"""

from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch
from uuid import uuid4

import pytest
from django.contrib.auth import get_user_model
from django.db import NotSupportedError, connection, transaction
from django.urls import reverse
from django.utils import timezone

from apps.notifications.models import WhatsAppDelivery
from apps.notifications.services.ultramsg import UltraMsgConnectionError
from apps.payments.models import PaymentAttempt
from apps.reservations.models import BookingModificationRequest, RefundObligation, Reservation
from apps.reservations.services.automatic_modifications import execute_automatic_modification
from apps.reservations.services.hostaway_modifications import (
    HostawayModificationService,
    _build_update_request,
)
from apps.reservations.services.modifications import ModificationService
from apps.reservations.services.owner_settlement import settlement_for
from tests.test_booking_modifications_phase6 import (
    ModificationAvailabilityStub,
    WriteClientStub,
    confirmed_reservation,
    create_extension,
    updated_snapshot,
)
from tests.test_hyperpay_refunds_phase94 import REFUND_SETTINGS, RefundStub

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def isolated_providers(settings):
    settings.HOSTAWAY_LIVE_MODIFICATION_ENABLED = True
    settings.HOSTAWAY_LIVE_EXTENSION_ENABLED = True
    settings.BOOKING_AUTOMATIC_MODIFICATION_APPROVAL = False
    settings.ULTRAMSG_ENABLED = True
    settings.ULTRAMSG_INSTANCE_ID = "instance-test"
    settings.ULTRAMSG_TOKEN = "test-token"
    settings.ACCOUNTING_WHATSAPP_NUMBER = "+966597193102"


@pytest.fixture
def owner_client(client):
    owner = get_user_model().objects.create_superuser(
        "rootfix-owner", "owner@example.invalid", "test-only"
    )
    client.force_login(owner)
    return client


def collect(reservation, amount):
    return PaymentAttempt.objects.create(
        booking_intent=reservation.booking_intent,
        provider="hyperpay",
        provider_payment_id=uuid4().hex,
        merchant_transaction_id=uuid4().hex,
        idempotency_key=uuid4().hex,
        amount=Decimal(amount),
        currency=reservation.currency,
        status=PaymentAttempt.Status.SUCCEEDED,
        verified_at=timezone.now(),
    )


def approve(client, reservation, modification, total):
    response = client.post(
        reverse("notifications:booking_detail", args=[reservation.pk]),
        {
            "action": "set_final_price",
            "modification_id": modification.pk,
            "final_total_price": total,
        },
    )
    assert response.status_code == 302
    modification.refresh_from_db()
    assert modification.new_total == Decimal(total)
    assert modification.status == BookingModificationRequest.Status.READY_FOR_HOSTAWAY


def test_postgres_reproduces_original_outer_join_error():
    if connection.vendor != "postgresql":
        pytest.skip("PostgreSQL regression control; SQLite ignores row locking")
    modification = create_extension(confirmed_reservation()).request
    with pytest.raises(NotSupportedError), transaction.atomic():
        BookingModificationRequest.objects.select_for_update().select_related(
            "reservation", "reservation__booking_intent"
        ).get(pk=modification.pk)


@pytest.mark.parametrize("total", ["400.00", "500.25", "700.00"])
def test_unpaid_owner_price_then_hostaway_then_full_amount_to_aseel_once(owner_client, total):
    reservation = confirmed_reservation()
    modification = create_extension(reservation).request
    approve(owner_client, reservation, modification, total)
    url = reverse("notifications:booking_detail", args=[reservation.pk])
    page = owner_client.get(url)
    assert page.status_code == 200
    assert page.context["increase_adjustment"].pk == modification.pk
    assert page.context["settlement"].due == Decimal(total)
    external = WriteClientStub(snapshot=updated_snapshot(modification))
    with (
        patch(
            "apps.reservations.services.automatic_modifications.HostawayModificationService",
            return_value=HostawayModificationService(client=external),
        ),
        patch(
            "apps.notifications.services.ultramsg.UltraMsgClient.send_text",
            return_value={"sent": "true", "id": "test-message"},
        ) as send,
    ):

        def accepted(**kwargs):
            modification.refresh_from_db()
            assert modification.status == BookingModificationRequest.Status.COMPLETED
            assert external.calls == 1
            return {"sent": "true", "id": "test-message"}

        send.side_effect = accepted
        payload = {
            "action": "send_modification_payment_link_request",
            "modification_id": modification.pk,
        }
        first = owner_client.post(url, payload)
        second = owner_client.post(url, payload)
    assert first.status_code == second.status_code == 302
    assert first.url == second.url == reverse("notifications:booking_list")
    assert external.calls == send.call_count == 1
    assert f"{Decimal(total):,.2f} SAR" in send.call_args.kwargs["body"]
    assert "/reservations/77001" in send.call_args.kwargs["body"]
    assert not RefundObligation.objects.exists()
    reservation.refresh_from_db()
    assert reservation.total_price == Decimal(total)
    assert reservation.check_out == modification.new_check_out


@pytest.mark.parametrize(
    ("paid", "total", "due", "refund"),
    [
        ("500.25", "700", "199.75", "0"),
        ("100", "400", "300", "0"),
        ("500.25", "400", "0", "100.25"),
        ("500.25", "500.25", "0", "0"),
    ],
)
def test_settlement_uses_actual_payment_not_old_total(owner_client, paid, total, due, refund):
    reservation = confirmed_reservation()
    collect(reservation, paid)
    modification = create_extension(reservation).request
    approve(owner_client, reservation, modification, total)
    actual = settlement_for(reservation, Decimal(total))
    assert actual.due == Decimal(due)
    assert actual.refund == Decimal(refund)


def test_hostaway_components_match_owner_total(owner_client):
    reservation = confirmed_reservation()
    modification = create_extension(reservation).request
    approve(owner_client, reservation, modification, "50")
    payload = _build_update_request(modification)
    assert payload.total_price == Decimal("50")
    assert sum(
        field.total
        for field in payload.finance_fields
        if field.is_included_in_total_price and not field.is_deleted
    ) == Decimal("50")
    assert any(field.is_overridden_by_user for field in payload.finance_fields)


def test_paid_decrease_updates_hostaway_before_actual_refund_service(owner_client, settings):
    for name, value in REFUND_SETTINGS.items():
        setattr(settings, name, value)
    settings.HYPERPAY_ENVIRONMENT = "production"
    settings.HYPERPAY_REFUNDS_PRODUCTION_ENABLED = True
    settings.BOOKING_AUTOMATIC_REFUND_ENABLED = True
    reservation = confirmed_reservation()
    collect(reservation, "500.25")
    modification = create_extension(reservation).request
    approve(owner_client, reservation, modification, "400")
    external = WriteClientStub(snapshot=updated_snapshot(modification))
    gateway = RefundStub({"id": "refund-test", "result": {"code": "000.100.110"}})
    gateway.close = lambda: None
    with (
        patch(
            "apps.reservations.services.automatic_modifications.HostawayModificationService",
            return_value=HostawayModificationService(client=external),
        ),
        patch("apps.payments.hyperpay.refunds.HyperPayClient", return_value=gateway),
    ):
        response = owner_client.post(
            reverse("notifications:booking_detail", args=[reservation.pk]),
            {
                "action": "execute_adjustment",
                "modification_id": modification.pk,
                "approved_refund_amount": "100.25",
                "confirm_external_modification": "on",
            },
        )
    assert response.status_code == 302
    assert external.calls == len(gateway.calls) == 1
    assert gateway.calls[0][1]["amount"] == "100.25"
    assert RefundObligation.objects.get().status == RefundObligation.Status.TRANSFERRED
    assert settlement_for(reservation, Decimal("400")).refund == 0


def test_hostaway_failure_sends_no_payment_request(owner_client):
    from apps.integrations.hostaway.exceptions import HostawayTimeoutError

    reservation = confirmed_reservation()
    modification = create_extension(reservation).request
    approve(owner_client, reservation, modification, "50")
    external = WriteClientStub(error=HostawayTimeoutError())
    with (
        patch(
            "apps.reservations.services.automatic_modifications.HostawayModificationService",
            return_value=HostawayModificationService(client=external),
        ),
        patch("apps.notifications.services.ultramsg.UltraMsgClient.send_text") as send,
    ):
        response = owner_client.post(
            reverse("notifications:booking_detail", args=[reservation.pk]),
            {
                "action": "send_modification_payment_link_request",
                "modification_id": modification.pk,
            },
        )
    assert response.status_code == 302
    assert not send.called
    modification.refresh_from_db()
    assert modification.status == BookingModificationRequest.Status.UNKNOWN


def test_whatsapp_failure_does_not_undo_or_repeat_hostaway_write(owner_client):
    reservation = confirmed_reservation()
    modification = create_extension(reservation).request
    approve(owner_client, reservation, modification, "50")
    external = WriteClientStub(snapshot=updated_snapshot(modification))
    with (
        patch(
            "apps.reservations.services.automatic_modifications.HostawayModificationService",
            return_value=HostawayModificationService(client=external),
        ),
        patch(
            "apps.notifications.services.ultramsg.UltraMsgClient.send_text",
            side_effect=UltraMsgConnectionError,
        ) as send,
    ):
        url = reverse("notifications:booking_detail", args=[reservation.pk])
        data = {
            "action": "send_modification_payment_link_request",
            "modification_id": modification.pk,
        }
        assert owner_client.post(url, data).status_code < 500
        assert owner_client.post(url, data).status_code < 500
    assert external.calls == send.call_count == 1
    modification.refresh_from_db()
    assert modification.status == BookingModificationRequest.Status.COMPLETED
    assert WhatsAppDelivery.objects.get().status == WhatsAppDelivery.Status.FAILED


def test_owner_search_never_resurrects_expired_or_superseded_proposal():
    reservation = confirmed_reservation()
    service = ModificationService(availability_service=ModificationAvailabilityStub(reservation))

    def search(nights):
        return service.create_extension_quote(
            reservation,
            new_check_out=reservation.check_out + timedelta(days=nights),
            session_hash="owner:test",
            owner_override=True,
        ).request

    first = search(2)
    second = search(3)
    first.expires_at = timezone.now() - timedelta(minutes=1)
    first.save(update_fields=["expires_at"])
    latest = search(2)
    assert len({first.pk, second.pk, latest.pk}) == 3
    second.refresh_from_db()
    assert second.status == BookingModificationRequest.Status.SUPERSEDED
    assert (
        ModificationService().set_owner_final_total(second, final_total=Decimal("50")).code
        != "priced"
    )
    assert not latest.is_expired


def test_owner_can_modify_again_after_completed_change(owner_client):
    reservation = confirmed_reservation()
    modification = create_extension(reservation).request
    approve(owner_client, reservation, modification, "50")
    outcome = execute_automatic_modification(
        modification,
        owner_override=True,
        service=HostawayModificationService(
            client=WriteClientStub(snapshot=updated_snapshot(modification))
        ),
    )
    assert outcome.code == "completed"
    reservation.refresh_from_db()
    assert reservation.normalized_status == Reservation.Status.MODIFIED
    service = ModificationService(availability_service=ModificationAvailabilityStub(reservation))
    proposal = service.create_extension_quote(
        reservation,
        new_check_out=reservation.check_out + timedelta(days=1),
        session_hash="owner:test",
        owner_override=True,
    )
    assert proposal.code == "created"


def test_direct_unapproved_post_cannot_send_or_update(owner_client):
    reservation = confirmed_reservation()
    modification = create_extension(reservation).request
    with (
        patch("apps.reservations.operations_views.execute_automatic_modification") as execute,
        patch("apps.notifications.services.ultramsg.UltraMsgClient.send_text") as send,
    ):
        response = owner_client.post(
            reverse("notifications:booking_detail", args=[reservation.pk]),
            {
                "action": "send_modification_payment_link_request",
                "modification_id": modification.pk,
            },
        )
    assert response.status_code < 500
    assert not execute.called and not send.called


def test_payment_arriving_after_approval_requires_a_fresh_settlement(owner_client):
    reservation = confirmed_reservation()
    modification = create_extension(reservation).request
    approve(owner_client, reservation, modification, "700")
    collect(reservation, "100")
    external = WriteClientStub(snapshot=updated_snapshot(modification))
    result = execute_automatic_modification(
        modification, owner_override=True, service=HostawayModificationService(client=external)
    )
    assert result.code == "payment_changed_reprice_required"
    assert external.calls == 0


def test_refund_test_mode_amount_validation_does_not_leave_a_fake_submission(settings):
    from apps.payments.hyperpay.exceptions import HyperPayRefundError
    from apps.payments.hyperpay.refunds import HyperPayRefundService
    from tests.test_hyperpay_refunds_phase94 import prepared_refund

    for name, value in REFUND_SETTINGS.items():
        setattr(settings, name, value)
    refund, _ = prepared_refund(Decimal("100.25"))
    gateway = RefundStub({})
    with pytest.raises(HyperPayRefundError, match="refund_amount_not_supported"):
        HyperPayRefundService(client=gateway).submit(refund, operator=None)
    refund.refresh_from_db()
    assert refund.status == RefundObligation.Status.DUE
    assert gateway.calls == []


def test_second_partial_refund_uses_remaining_original_payment(settings):
    from apps.payments.hyperpay.refunds import HyperPayRefundService
    from apps.reservations.services.refunds import RefundComputation, record_obligation
    from tests.test_hyperpay_refunds_phase94 import prepared_refund

    for name, value in REFUND_SETTINGS.items():
        setattr(settings, name, value)
    first, payment = prepared_refund(Decimal("400"))
    gateway = RefundStub({"id": "first-refund", "result": {"code": "000.100.110"}})
    service = HyperPayRefundService(client=gateway)
    service.submit(first, operator=None)
    modification = create_extension(first.reservation).request
    second = record_obligation(
        first.reservation,
        reason=RefundObligation.Reason.MODIFICATION_DECREASE,
        modification=modification,
        computation=RefundComputation(Decimal("100"), "SAR"),
    )
    service.submit(second, operator=None)
    assert len(gateway.calls) == 2
    assert gateway.calls[-1][0] == payment.provider_payment_id
