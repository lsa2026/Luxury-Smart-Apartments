from unittest.mock import patch

import pytest
from django.utils import timezone

from apps.notifications.models import EmailDelivery, WhatsAppDelivery
from apps.notifications.services import accounting_email as accounting
from apps.notifications.services.ultramsg import send_manual_payment_link_request
from tests.test_account_booking_claim import make_reservation

pytestmark = pytest.mark.django_db


@pytest.fixture
def stay(settings):
    settings.EMAIL_DELIVERY_ENABLED = True
    settings.OPERATIONS_EMAIL = accounting.BUSINESS_EMAIL
    settings.ULTRAMSG_ENABLED = True
    row = make_reservation("SYNTHETIC-ACCOUNTING-EMAIL")
    row.hostaway_reservation_id = 990001
    row.save(update_fields=["hostaway_reservation_id"])
    return row


def test_one_email_to_business_mailbox_is_fenced_before_send_and_not_a_collection(stay):
    before = (stay.payment_status, stay.total_price, stay.normalized_status)
    observed = []

    def send(message, **kwargs):
        row = EmailDelivery.objects.get(idempotency_key=accounting.key(stay))
        assert row.status == "sending" and row.attempt_count == 1
        assert message.to == [accounting.BUSINESS_EMAIL]
        assert "https://dashboard.hostaway.com/reservations/990001" in message.body
        observed.append(message)
        return 1

    with (
        patch(
            "apps.notifications.services.accounting_email.EmailMessage.send",
            autospec=True,
            side_effect=send,
        ),
        patch("apps.notifications.services.ultramsg.UltraMsgClient.send_text") as old,
    ):
        result = send_manual_payment_link_request(reservation_id=stay.pk)
        again = send_manual_payment_link_request(reservation_id=stay.pk)
    assert result.code == "email_sent" and again.code == "email_already_sent"
    assert len(observed) == 1 and old.call_count == 0
    assert EmailDelivery.objects.count() == 1 and not WhatsAppDelivery.objects.exists()
    assert result.delivery.status == "sent" and result.delivery.provider_message_id is None
    assert result.delivery.recipient_hash == accounting.recipient_hmac(accounting.BUSINESS_EMAIL)
    assert "Guest" not in str(result.delivery.__dict__)
    stay.refresh_from_db()
    assert (stay.payment_status, stay.total_price, stay.normalized_status) == before
    assert not stay.payment_requests.exists()


@pytest.mark.parametrize("result", [TimeoutError("synthetic"), 0])
def test_unknown_email_cannot_be_replayed_or_fall_back(stay, result):
    with patch("apps.notifications.services.accounting_email.EmailMessage.send") as send:
        if isinstance(result, Exception):
            send.side_effect = result
        else:
            send.return_value = result
        first = send_manual_payment_link_request(reservation_id=stay.pk)
        second = send_manual_payment_link_request(reservation_id=stay.pk)
    assert first.code == second.code == "email_unknown" and send.call_count == 1
    assert first.delivery.last_error_code == "accounting_email_outcome_unknown"
    assert first.delivery.status == "failed" and first.delivery.attempt_count == 1
    assert not WhatsAppDelivery.objects.exists()


@pytest.mark.parametrize("state", ["queued", "sending", "sent", "failed", "disabled"])
def test_legacy_attempt_is_retained_and_never_rearmed_as_email(stay, state):
    WhatsAppDelivery.objects.create(
        reservation=stay,
        message_type="manual_payment_link_request",
        status=state,
        recipient_masked="old",
        recipient_reference="accounting",
        queued_at=timezone.now(),
        idempotency_key="manual-payment-link:" + stay.public_reference,
    )
    with patch("apps.notifications.services.accounting_email.EmailMessage.send") as send:
        result = send_manual_payment_link_request(reservation_id=stay.pk)
    assert result.code == "legacy_review_required" and send.call_count == 0
    assert not EmailDelivery.objects.exists()
    assert WhatsAppDelivery.objects.get().status == state


@pytest.mark.parametrize(
    "setting,value",
    [
        ("EMAIL_DELIVERY_ENABLED", False),
        ("OPERATIONS_EMAIL", ""),
        ("OPERATIONS_EMAIL", "another@example.invalid"),
    ],
)
def test_existing_mail_controls_and_exact_owner_destination_are_required(
    stay, settings, setting, value
):
    setattr(settings, setting, value)
    with patch("apps.notifications.services.accounting_email.EmailMessage.send") as send:
        result = send_manual_payment_link_request(reservation_id=stay.pk)
    assert result.code == "email_disabled" and send.call_count == 0
    assert result.delivery.attempt_count == 0


def test_generic_email_retry_cannot_repeat_an_accounting_attempt(stay):
    from apps.notifications.services.email import send_queued_email

    with patch(
        "apps.notifications.services.accounting_email.EmailMessage.send", side_effect=TimeoutError
    ):
        first = send_manual_payment_link_request(reservation_id=stay.pk)
    EmailDelivery.objects.filter(pk=first.delivery.pk).update(status="queued")
    with patch("apps.notifications.services.email.DjangoEmailProvider.send") as send:
        result = send_queued_email(first.delivery.pk)
    assert result.code == "accounting_delivery_owned" and send.call_count == 0
    assert EmailDelivery.objects.get().attempt_count == 1


def test_build_failure_has_no_external_attempt_and_retains_original_delivery(stay):
    with (
        patch(
            "apps.notifications.services.ultramsg._manual_payment_message", side_effect=ValueError
        ),
        patch("apps.notifications.services.accounting_email.EmailMessage.send") as send,
    ):
        result = send_manual_payment_link_request(reservation_id=stay.pk)
        again = send_manual_payment_link_request(reservation_id=stay.pk)
    assert result.code == "email_failed" and again.code == "email_previously_failed"
    assert (
        send.call_count == 0 and result.delivery.last_error_code == "accounting_email_build_failed"
    )
