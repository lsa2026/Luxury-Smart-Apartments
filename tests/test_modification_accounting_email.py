"""One-shot modification accounting alerts, without external sending."""

from datetime import timedelta
from unittest.mock import patch
from uuid import uuid4

import pytest
from django.db import connection
from django.utils import timezone

from apps.notifications.models import EmailDelivery, WhatsAppDelivery
from apps.notifications.services import accounting_email as accounting
from apps.notifications.services.ultramsg import (
    send_modification_payment_link_request as send_change,
)
from apps.reservations.models import BookingModificationRequest
from tests.test_account_booking_claim import make_reservation

pytestmark = pytest.mark.django_db


@pytest.fixture
def change(settings):
    settings.EMAIL_DELIVERY_ENABLED = True
    settings.OPERATIONS_EMAIL = accounting.BUSINESS_EMAIL
    settings.ULTRAMSG_ENABLED = True
    stay = make_reservation("SYN-MOD-" + uuid4().hex[:20])
    stay.hostaway_reservation_id = 990002
    stay.save(update_fields=["hostaway_reservation_id"])
    return BookingModificationRequest.objects.create(
        reservation=stay,
        request_type="change_dates",
        status="awaiting_payment",
        old_check_in=stay.check_in,
        old_check_out=stay.check_out,
        new_check_in=stay.check_in,
        new_check_out=stay.check_out + timedelta(days=1),
        old_guests=stay.guests,
        new_guests=stay.guests,
        old_total=stay.total_price,
        new_total=stay.total_price + 50,
        price_difference=50,
        currency=stay.currency,
        quote_snapshot={},
        idempotency_key="synthetic-change:" + uuid4().hex,
        session_key_hash="synthetic-session",
        expires_at=timezone.now() + timedelta(days=1),
    )


def test_committed_attempt_and_real_content_without_financial_effect(change):
    stay = change.reservation
    before = (stay.total_price, stay.payment_status, stay.normalized_status, change.status)

    def accepted(message, **kwargs):
        row = accounting.status_for_modification(change)
        assert row.status == "sending" and row.attempt_count == 1
        assert message.to == [accounting.BUSINESS_EMAIL]
        assert "فرق تعديل حجز" in message.body and "/reservations/990002" in message.body
        assert "50.00" in message.body
        return 1

    with (
        patch(
            "apps.notifications.services.accounting_email.EmailMessage.send",
            autospec=True,
            side_effect=accepted,
        ) as mail,
        patch("apps.notifications.services.ultramsg.UltraMsgClient.send_text") as legacy,
    ):
        result = send_change(modification_id=change.pk)
        again = send_change(modification_id=change.pk)
    assert result.code == "email_sent" and again.code == "email_already_sent"
    assert mail.call_count == 1 and legacy.call_count == 0
    assert result.delivery.recipient_reference == str(change.pk)
    assert result.delivery.provider_message_id is None
    assert not WhatsAppDelivery.objects.exists()
    stay.refresh_from_db()
    change.refresh_from_db()
    assert (stay.total_price, stay.payment_status, stay.normalized_status, change.status) == before
    assert not stay.payment_requests.exists()


@pytest.mark.parametrize("state", ["queued", "sending", "sent", "failed", "disabled"])
def test_any_old_provider_row_prevents_email_adoption(change, state):
    old = WhatsAppDelivery.objects.create(
        modification_request=change,
        message_type="modification_payment_link_request",
        recipient_masked="old",
        recipient_reference="accounting",
        status=state,
        queued_at=timezone.now(),
        idempotency_key="legacy:" + uuid4().hex,
    )
    with patch("apps.notifications.services.accounting_email.EmailMessage.send") as mail:
        result = send_change(modification_id=change.pk)
    assert result.code == "legacy_review_required" and result.delivery.pk == old.pk
    assert mail.call_count == 0 and not EmailDelivery.objects.exists()
    old.refresh_from_db()
    assert old.status == state


@pytest.mark.parametrize("failure", [TimeoutError(), 0])
def test_ambiguous_smtp_and_generic_retry_never_repeat(change, failure):
    from apps.notifications.services.email import send_queued_email

    with patch("apps.notifications.services.accounting_email.EmailMessage.send") as mail:
        if isinstance(failure, Exception):
            mail.side_effect = failure
        else:
            mail.return_value = failure
        first = send_change(modification_id=change.pk)
        again = send_change(modification_id=change.pk)
    assert first.code == again.code == "email_unknown" and mail.call_count == 1
    EmailDelivery.objects.filter(pk=first.delivery.pk).update(status="queued")
    with patch("apps.notifications.services.email.DjangoEmailProvider.send") as generic:
        result = send_queued_email(first.delivery.pk)
    assert result.code == "accounting_delivery_owned" and generic.call_count == 0
    assert EmailDelivery.objects.get().attempt_count == 1


@pytest.mark.parametrize("guard", ["no_amount", "owner_not_confirmed", "wrong_destination"])
def test_original_financial_and_destination_guards_remain(change, settings, guard):
    if guard == "no_amount":
        change.price_difference = 0
    elif guard == "owner_not_confirmed":
        change.quote_snapshot = {"owner_final_total": str(change.new_total)}
    else:
        settings.OPERATIONS_EMAIL = "wrong@example.invalid"
    change.save()
    with patch("apps.notifications.services.accounting_email.EmailMessage.send") as mail:
        if guard == "no_amount":
            with pytest.raises(ValueError):
                send_change(modification_id=change.pk)
        else:
            assert send_change(modification_id=change.pk).code != "email_sent"
    assert mail.call_count == 0
    if guard != "wrong_destination":
        assert not EmailDelivery.objects.exists()


@pytest.mark.django_db(transaction=True)
@pytest.mark.skipif(connection.vendor != "postgresql", reason="Requires real PostgreSQL locks")
def test_concurrent_click_cannot_make_another_attempt_after_smtp_fence(change):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    from django.db import close_old_connections

    entered, release = Event(), Event()
    pk = change.pk

    def send_in_thread():
        close_old_connections()
        try:
            return send_change(modification_id=pk)
        finally:
            close_old_connections()

    def smtp(**kwargs):
        entered.set()
        assert release.wait(5)
        return 1

    with patch(
        "apps.notifications.services.accounting_email.EmailMessage.send", side_effect=smtp
    ) as mail:
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(send_in_thread)
            assert entered.wait(3)
            try:
                assert send_change(modification_id=pk).code == "email_unknown"
                assert EmailDelivery.objects.get().attempt_count == 1
            finally:
                release.set()
            assert future.result(timeout=3).code == "email_sent"
    assert mail.call_count == 1
