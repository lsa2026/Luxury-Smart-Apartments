"""Local queue, consent and multilingual rendering; never contacts a mail server."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from django.core import mail
from django.template.loader import render_to_string
from django.utils import timezone

from apps.loyalty.models import Ledger
from apps.loyalty.notifications import dispatch_due, queue_movement, resolve_recipient
from apps.loyalty.services import issue_invite, reconcile
from apps.notifications.models import EmailDelivery
from apps.notifications.services.email import EmailProviderError, send_queued_email
from tests.test_loyalty_stage8 import booking, join, user

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def program(settings, monkeypatch):
    settings.LOYALTY_ENABLED = settings.LOYALTY_MESSAGES_ENABLED = True
    settings.LOYALTY_LAUNCH_AT = "2026-10-01T00:00:00+03:00"
    settings.LOYALTY_TERMS_APPROVED = settings.EMAIL_DELIVERY_ENABLED = True
    settings.LOYALTY_TERMS_VERSION = "test-only"
    settings.LOYALTY_TERMS_REFERENCE = "/test-only-terms/"
    clock = [datetime(2026, 10, 10, 13, tzinfo=UTC)]
    monkeypatch.setattr(timezone, "now", lambda: clock[0])
    return clock


def movement(language="ar"):
    u = user(f"notice-{language}")
    m = join(u)
    m.language = language
    m.save()
    r = booking(u)
    reconcile(r.pk)
    return Ledger.objects.get(member=m, reservation=r)


@pytest.mark.parametrize("language", ["ar", "en", "fr"])
def test_movement_once_in_guests_language_with_actual_statement_values(language):
    row = movement(language)
    queued = queue_movement(row.pk)
    assert queue_movement(row.pk).pk == queued.pk
    assert queued.language == language and queued.template_name == "loyalty"
    recipient, context = resolve_recipient(queued)
    assert recipient == row.member.customer.email
    assert context["credit"] == "22.50" and context["debit"] == "0.00"
    assert Decimal(context["balance"]) == Decimal("22.50")
    assert send_queued_email(queued.pk).sent
    assert not send_queued_email(queued.pk).sent
    assert len(mail.outbox) == 1 and "22.50" in mail.outbox[0].body
    assert row.operation_number in mail.outbox[0].alternatives[0].content
    if language == "ar":
        assert 'dir="rtl"' in mail.outbox[0].alternatives[0].content


def test_messages_off_never_queue_or_send(settings):
    row = movement()
    settings.LOYALTY_MESSAGES_ENABLED = False
    assert queue_movement(row.pk) is None
    assert dispatch_due()["status"] == "disabled"
    assert not EmailDelivery.objects.exclude(message_type="loyalty_welcome").exists()


def test_unpublished_or_unsafe_terms_never_send_promotions(settings):
    row = movement()
    settings.LOYALTY_TERMS_REFERENCE = "//unsafe.invalid/terms"
    assert queue_movement(row.pk) is None
    assert dispatch_due()["status"] == "disabled"


def test_invitation_after_actual_arrival_plus_hour_requires_marketing_permission(program):
    u = user("invitation")
    r = booking(u)
    r.booking_intent.marketing_consent = True
    r.booking_intent.language = "fr"
    r.booking_intent.save()
    assert dispatch_due()["queued"] == 0
    program[0] += timedelta(minutes=10)
    assert dispatch_due()["queued"] == 1
    assert dispatch_due()["queued"] == 0
    queued = EmailDelivery.objects.get(message_type="loyalty_join_invitation")
    assert queued.language == "fr"
    assert resolve_recipient(queued)[0] == r.booking_intent.guest_email
    r.booking_intent.marketing_consent = False
    r.booking_intent.save()
    with pytest.raises(EmailProviderError):
        resolve_recipient(queued)


def test_arrival_not_assumed_from_scheduled_date(program):
    r = booking(user("no-arrival"), arrived=False)
    r.booking_intent.marketing_consent = True
    r.booking_intent.save()
    program[0] += timedelta(hours=2)
    assert dispatch_due()["queued"] == 0


def test_cancelled_booking_invitation_blocked_at_send_time(program):
    r = booking(user("cancelled-invite"))
    r.booking_intent.marketing_consent = True
    r.booking_intent.save()
    program[0] += timedelta(minutes=10)
    dispatch_due()
    queued = EmailDelivery.objects.get(message_type="loyalty_join_invitation")
    r.normalized_status = "cancelled"
    r.save()
    assert send_queued_email(queued.pk).code == "loyalty_invitation_not_eligible"
    assert not mail.outbox


def test_recovery_queues_committed_movement_without_posting_it_again():
    row = movement()
    assert dispatch_due()["queued"] == 1
    assert dispatch_due()["queued"] == 0
    assert Ledger.objects.count() == 1
    assert EmailDelivery.objects.get(message_type="loyalty_movement").recipient_reference == str(
        row.pk
    )


def test_ready_notice_recovers_even_when_invitation_already_issued_in_ui(program):
    u = user("already-issued")
    member = join(u)
    r = booking(u)
    reconcile(r.pk)
    program[0] += timedelta(days=3)
    issue_invite(u, r.pk)
    dispatch_due()
    assert EmailDelivery.objects.filter(message_type="loyalty_invite_ready").count() == 1
    dispatch_due()
    assert EmailDelivery.objects.filter(message_type="loyalty_invite_ready").count() == 1
    member.refresh_from_db()
    assert member.first_completed_reservation_id == r.pk


def test_incomplete_receipt_never_issues_invitation(program):
    u = user("unpaid-completion")
    member = join(u)
    booking(u, paid=False)
    program[0] += timedelta(days=3)
    assert dispatch_due()["queued"] == 0
    member.refresh_from_db()
    assert not member.invitation_token


def test_withdrawn_member_does_not_receive_stale_movement_notice():
    row = movement()
    queued = queue_movement(row.pk)
    row.member.status = "withdrawn"
    row.member.save()
    assert send_queued_email(queued.pk).code == "loyalty_membership_inactive"
    assert not mail.outbox


def test_plain_and_html_templates_include_same_audit_details():
    for lang in ("ar", "en", "fr"):
        context = {"operation": "TEST-OP", "credit": "1.00", "debit": "0.00", "balance": "20.00"}
        for suffix in ("loyalty.html", "message.txt"):
            text = render_to_string(f"emails/{lang}/{suffix}", context)
            assert "TEST-OP" in text and "20.00" in text
