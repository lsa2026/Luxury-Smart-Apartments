"""Exercise real owner POSTs and database locks; only external providers are stubbed.

Run this file on PostgreSQL in CI as well as SQLite. The original 500 cannot be
detected by SQLite, which silently omits SELECT FOR UPDATE.
"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from html.parser import HTMLParser
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
    price_quote,
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


def owner_execution_service(client, reservation):
    return HostawayModificationService(
        client=client,
        modification_service=ModificationService(
            availability_service=ModificationAvailabilityStub(reservation)
        ),
    )


class ExistingStayAvailability(ModificationAvailabilityStub):
    """Hostaway has occupied original nights, but a free added night."""

    def fetch_calendar(self, **kwargs):
        assert kwargs["start_date"] == self.reservation.check_out
        assert kwargs["end_date"] == self.reservation.check_out + timedelta(days=1)
        return super().fetch_calendar(**kwargs)

    def check(self, *args, **kwargs):
        raise AssertionError("An existing reservation must not check its own occupied nights")

    def calculate_price(self, listing_id, **kwargs):
        assert listing_id == self.reservation.property.hostaway_listing_id
        assert kwargs["check_in"] == self.reservation.check_in
        assert kwargs["check_out"] == self.reservation.check_out + timedelta(days=1)
        assert kwargs["guests"] == self.reservation.guests
        assert kwargs["bypass_currency_cache"] is True
        return super().calculate_price(listing_id, **kwargs)


def extension_from_owner_form(owner_client, reservation, *, day_changes=None):
    availability = ExistingStayAvailability(
        reservation,
        quote=price_quote(reservation, check_out=reservation.check_out + timedelta(days=1)),
        day_changes=day_changes,
    )
    with patch(
        "apps.reservations.operations_views.ModificationService",
        return_value=ModificationService(availability_service=availability),
    ):
        response = owner_client.post(
            reverse("notifications:booking_detail", args=[reservation.pk]),
            {
                "action": "quote_change",
                "new_check_in": reservation.check_in.isoformat(),
                "new_check_out": (reservation.check_out + timedelta(days=1)).isoformat(),
                "new_guests": reservation.guests,
            },
        )
    return response, availability


def test_unpaid_owner_date_form_prices_only_added_inventory_and_keeps_booking(owner_client):
    reservation = confirmed_reservation()
    original = (reservation.check_in, reservation.check_out, reservation.total_price)
    # A one-night addition is valid for an existing two-night stay, even when
    # a new arrival on that date would be closed or require two nights.
    response, availability = extension_from_owner_form(
        owner_client,
        reservation,
        day_changes={0: {"minimum_stay": 2, "closed_on_arrival": True}},
    )
    assert response.status_code == 302
    proposal = BookingModificationRequest.objects.get(reservation=reservation)
    assert proposal.request_type == BookingModificationRequest.RequestType.EXTEND_STAY
    assert proposal.new_check_out == reservation.check_out + timedelta(days=1)
    assert proposal.new_total == Decimal("650.25")
    assert availability.calendar_calls == availability.price_calls == 1
    assert not proposal.hostaway_operations.exists()
    assert not PaymentAttempt.objects.exists()
    assert not WhatsAppDelivery.objects.exists()
    reservation.refresh_from_db()
    assert (reservation.check_in, reservation.check_out, reservation.total_price) == original


@pytest.mark.parametrize(
    ("changes", "reason"),
    [
        ({0: {"is_available": False, "available_units_to_sell": 0}}, "لا تتوفر"),
        ({1: {"closed_on_departure": True}}, "المغادرة غير مسموحة"),
        ({0: {"maximum_stay": 2}}, "الحد الأقصى"),
    ],
)
def test_owner_date_form_preserves_conflicts_and_explains_refusal(owner_client, changes, reason):
    reservation = confirmed_reservation()
    response, availability = extension_from_owner_form(
        owner_client, reservation, day_changes=changes
    )
    assert response.status_code == 200
    assert reason in response.content.decode()
    assert not BookingModificationRequest.objects.exists()
    assert availability.price_calls == 0


@pytest.mark.parametrize("failure", ["unavailable", "price", "currency", "timeout"])
def test_owner_extension_rechecks_before_any_hostaway_write(owner_client, failure):
    from dataclasses import replace

    from apps.integrations.hostaway.exceptions import HostawayTimeoutError

    reservation = confirmed_reservation()
    extension_from_owner_form(owner_client, reservation)
    proposal = BookingModificationRequest.objects.get(reservation=reservation)
    approve(owner_client, reservation, proposal, "700")
    original_out = reservation.check_out
    current_quote = price_quote(reservation, check_out=proposal.new_check_out)
    if failure == "price":
        current_quote = replace(current_quote, total_price=Decimal("701"))
    elif failure == "currency":
        current_quote = replace(current_quote, currency="USD")
    availability = ExistingStayAvailability(
        reservation,
        quote=current_quote,
        day_changes=(
            {0: {"is_available": False, "available_units_to_sell": 0}}
            if failure == "unavailable"
            else None
        ),
    )
    if failure == "timeout":
        availability.fetch_calendar = lambda **kwargs: (_ for _ in ()).throw(HostawayTimeoutError())
    external = WriteClientStub(snapshot=updated_snapshot(proposal))
    outcome = HostawayModificationService(
        client=external,
        modification_service=ModificationService(availability_service=availability),
    ).execute(proposal)
    assert (
        outcome.code
        == {
            "unavailable": "unavailable_dates",
            "price": "price_changed",
            "currency": "currency_changed",
            "timeout": "hostaway_temporarily_unavailable",
        }[failure]
    )
    assert external.calls == 0
    assert not proposal.hostaway_operations.exists()
    reservation.refresh_from_db()
    assert reservation.check_out == original_out
    assert not PaymentAttempt.objects.exists()
    assert not WhatsAppDelivery.objects.exists()


def test_extension_payment_revalidation_ignores_occupied_original_nights(owner_client):
    reservation = confirmed_reservation()
    extension_from_owner_form(owner_client, reservation)
    proposal = BookingModificationRequest.objects.get(reservation=reservation)
    availability = ExistingStayAvailability(
        reservation, quote=price_quote(reservation, check_out=proposal.new_check_out)
    )
    outcome = ModificationService(availability_service=availability).revalidate_for_payment(
        proposal
    )
    assert outcome.code == "ready"
    assert availability.calendar_calls == availability.price_calls == 1


@pytest.mark.parametrize(("same_day_allowed", "hour"), [(False, 8), (True, 8), (False, 13)])
def test_existing_arrival_policy_does_not_block_owner_extension(
    owner_client, settings, same_day_allowed, hour
):
    settings.BOOKING_LAUNCH_FLEXIBLE_CANCELLATION_ENABLED = True
    # B12's observed local policy: same-day arrivals disabled, 12h lead,
    # 15:00 check-in. The existing arrival is unchanged, even after 15:00.
    with patch("django.utils.timezone.now", return_value=datetime(2026, 10, 28, hour, tzinfo=UTC)):
        reservation = confirmed_reservation()
        reservation.check_in = timezone.localdate()
        reservation.check_out = reservation.check_in + timedelta(days=2)
        reservation.save(update_fields=["check_in", "check_out"])
        property_obj = reservation.property
        property_obj.allow_same_day_booking = same_day_allowed
        property_obj.same_day_booking_lead_time_hours = 12
        property_obj.check_in_time_start = 15
        property_obj.time_zone_name = "Asia/Riyadh"
        property_obj.min_nights = 1
        property_obj.max_nights = 365
        property_obj.save()
        response, availability = extension_from_owner_form(owner_client, reservation)
        assert response.status_code == 302
        proposal = BookingModificationRequest.objects.get(reservation=reservation)
        assert proposal.new_check_in == reservation.check_in
        assert proposal.new_check_out == reservation.check_out + timedelta(days=1)
        assert proposal.request_type == BookingModificationRequest.RequestType.EXTEND_STAY
        assert availability.calendar_calls == availability.price_calls == 1
        assert not PaymentAttempt.objects.exists()
        assert not proposal.hostaway_operations.exists()
        assert not WhatsAppDelivery.objects.exists()


def test_changed_owner_arrival_still_obeys_same_day_policy():
    with patch("django.utils.timezone.now", return_value=datetime(2026, 10, 28, 8, tzinfo=UTC)):
        reservation = confirmed_reservation()
        reservation.property.allow_same_day_booking = False
        reservation.property.save()
        availability = ModificationAvailabilityStub(reservation)
        result = ModificationService(availability_service=availability).create_change_quote(
            reservation,
            new_check_in=timezone.localdate(),
            new_check_out=timezone.localdate() + timedelta(days=3),
            new_guests=reservation.guests,
            session_hash="owner:test",
            owner_override=True,
        )
        assert result.code == "same_day_change_not_allowed"
        assert result.request is None
        assert availability.calendar_calls == availability.price_calls == 0


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


@pytest.mark.parametrize("system_total", ["2142.0000", "2142.5000", "0.0000"])
def test_owner_can_submit_the_price_rendered_by_the_booking_page(owner_client, system_total):
    reservation = confirmed_reservation()
    quote = price_quote(reservation, total=Decimal(system_total))
    proposal = create_extension(
        reservation, stub=ModificationAvailabilityStub(reservation, quote=quote)
    ).request
    url = reverse("notifications:booking_detail", args=[reservation.pk])
    page = owner_client.get(url)

    class PriceInput(HTMLParser):
        value = None

        def handle_starttag(self, tag, attrs):
            fields = dict(attrs)
            if tag == "input" and fields.get("name") == "final_total_price":
                self.value = fields.get("value")

    field = PriceInput()
    field.feed(page.content.decode())
    assert field.value == format(Decimal(system_total), ".2f")
    response = owner_client.post(
        url,
        {
            "action": "set_final_price",
            "modification_id": proposal.pk,
            "final_total_price": field.value,
        },
    )
    assert response.status_code == 302
    proposal.refresh_from_db()
    assert proposal.quote_snapshot["owner_final_total"] == field.value
    assert proposal.status == BookingModificationRequest.Status.READY_FOR_HOSTAWAY
    assert not proposal.hostaway_operations.exists()
    assert not PaymentAttempt.objects.exists()


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
            return_value=owner_execution_service(external, reservation),
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
            return_value=owner_execution_service(external, reservation),
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
            return_value=owner_execution_service(external, reservation),
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
            return_value=owner_execution_service(external, reservation),
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
        service=owner_execution_service(
            WriteClientStub(snapshot=updated_snapshot(modification)), reservation
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
        modification, owner_override=True, service=owner_execution_service(external, reservation)
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
