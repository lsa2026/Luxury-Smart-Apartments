"""Real source capture transactions, fixed future slots and existing relay only."""

from datetime import timedelta

import pytest
from django.db import transaction
from django.utils import timezone

from apps.reservations import message_source_relay as relay
from apps.reservations import website_review_source_v2 as review
from apps.reservations.review_source_models import WebsiteReviewCampaign, WebsiteReviewSlot
from tests import test_message_source_authorization as original
from tests import test_website_review_source_v2 as consumer

record = original.record
campaign, prepared = consumer.campaign, consumer.prepared
pytestmark = pytest.mark.django_db


def configure(record, settings):
    settings.MESSAGE_SOURCE_RELAY_ENABLED = True
    settings.MESSAGE_SOURCE_V2_ENABLED = True
    settings.MESSAGE_SOURCE_AUTH_ENABLED = True
    settings.WEBSITE_GUEST_MESSAGE_TRANSPORT = "ycloud_central"
    settings.MESSAGE_SOURCE_CUTOVER_AT = (record[2].created_at - timedelta(hours=1)).isoformat()
    settings.MESSAGE_REVIEW_SOURCE_V2_ENABLED = True
    settings.MESSAGE_REVIEW_SOURCE_CUTOVER_AT = (
        record[2].created_at - timedelta(seconds=1)
    ).isoformat()


def jobs():
    return relay.WebsiteMessageRelay.objects.filter(message_type__in=review.KINDS)


def test_new_verified_payment_captures_three_fixed_jobs_without_network(record, settings):
    configure(record, settings)
    scenario, stay, payment = original.paid_record(record)
    item = WebsiteReviewCampaign.objects.get()
    assert item.origin_event_id == record[2].pk
    assert item.source_occurred_at == record[2].created_at
    assert item.slots.count() == jobs().count() == 3
    for slot in item.slots.all():
        row = jobs().get(event_id=slot.pk)
        assert row.not_before == slot.not_before and row.expires_at == slot.expires_at
        assert row.source_occurred_at == record[2].created_at
        assert row.state == "pending" and not row.request_id
    with transaction.atomic():
        again = original.source.SOURCES[scenario][5].capture_payment(stay.pk)
    assert again.pk == payment.pk and jobs().count() == 3


@pytest.mark.parametrize(
    "blocked",
    [
        "disabled",
        "future",
        "missing",
        "naive",
        "malformed",
        "general_pause",
        "v2_pause",
        "foreign_transport",
    ],
)
def test_closed_controls_never_register_a_campaign_or_backfill_on_enable(record, settings, blocked):
    configure(record, settings)
    if blocked == "disabled":
        settings.MESSAGE_REVIEW_SOURCE_V2_ENABLED = False
    elif blocked == "future":
        settings.MESSAGE_REVIEW_SOURCE_CUTOVER_AT = (
            timezone.now() + timedelta(hours=1)
        ).isoformat()
    elif blocked == "missing":
        settings.MESSAGE_REVIEW_SOURCE_CUTOVER_AT = ""
    elif blocked == "naive":
        settings.MESSAGE_REVIEW_SOURCE_CUTOVER_AT = "2026-11-24T00:00:00"
    elif blocked == "malformed":
        settings.MESSAGE_REVIEW_SOURCE_CUTOVER_AT = "SYNTHETIC_SECRET_MARKER"
    elif blocked == "general_pause":
        settings.MESSAGE_SOURCE_RELAY_ENABLED = False
    elif blocked == "v2_pause":
        settings.MESSAGE_SOURCE_V2_ENABLED = False
    else:
        settings.WEBSITE_GUEST_MESSAGE_TRANSPORT = "ultramsg"
    scenario, stay, _ = original.paid_record(record)
    assert not WebsiteReviewCampaign.objects.exists() and not jobs().exists()
    configure(record, settings)
    # The existing payment's idempotent return is not a new event producer.
    with transaction.atomic():
        original.source.SOURCES[scenario][5].capture_payment(stay.pk)
    assert not WebsiteReviewCampaign.objects.exists() and not jobs().exists()


def test_new_paid_core_event_and_repeated_callbacks_keep_one_permanent_campaign(record, settings):
    original.paid_record(record)
    configure(record, settings)
    scenario, stay, _ = record
    comms = original.source.SOURCES[scenario][4]
    type(stay).objects.filter(pk=stay.pk).update(guest_name="Synthetic changed name")
    with transaction.atomic():
        comms.capture_event(stay.pk)
    item = WebsiteReviewCampaign.objects.get()
    old_ids = set(jobs().values_list("pk", flat=True))
    assert len(old_ids) == 3
    with transaction.atomic():
        comms.capture_event(stay.pk)
    assert set(jobs().values_list("pk", flat=True)) == old_ids
    jobs().update(state="uncertain", request_id="synthetic-uncertain")
    type(stay).objects.filter(pk=stay.pk).update(guest_phone="+16502530001")
    with transaction.atomic():
        comms.capture_event(stay.pk)
    assert WebsiteReviewCampaign.objects.get().pk == item.pk
    assert set(jobs().values_list("pk", flat=True)) == old_ids
    assert all(row.state == "uncertain" for row in jobs())


def test_an_old_core_is_not_adopted_by_a_new_payment(record, settings):
    configure(record, settings)
    settings.MESSAGE_REVIEW_SOURCE_CUTOVER_AT = timezone.now().isoformat()
    original.paid_record(record)
    assert not WebsiteReviewCampaign.objects.exists() and not jobs().exists()


def test_source_transaction_rollback_leaves_no_review_jobs(record, settings):
    configure(record, settings)
    with pytest.raises(RuntimeError), transaction.atomic():
        original.paid_record(record)
        assert jobs().count() == 3
        raise RuntimeError("synthetic original transaction rollback")
    assert not WebsiteReviewCampaign.objects.exists() and not WebsiteReviewSlot.objects.exists()
    assert not jobs().exists()
    record[1].refresh_from_db()
    assert record[1].paid_at is None


def test_closed_review_worker_leaves_pending_jobs_without_http_or_new_identity(
    campaign, settings, monkeypatch
):
    for slot in campaign[3].slots.all():
        review.register_slot(slot.pk)
    relay.WebsiteMessageRelay.objects.exclude(message_type__in=review.KINDS).update(state="held")
    original_ids = set(jobs().values_list("pk", flat=True))
    due = campaign[3].slots.get(step=1).not_before + timedelta(seconds=1)
    monkeypatch.setattr(timezone, "now", lambda: due)
    settings.MESSAGE_REVIEW_SOURCE_V2_ENABLED = False

    class NoNetwork:
        def post(self, *a, **kw):
            pytest.fail("a paused review cannot perform HTTP")

    assert relay.forward_due(client=NoNetwork()) == {"accepted": 0}
    assert set(jobs().values_list("pk", flat=True)) == original_ids
    assert all(row.state == "pending" for row in jobs())


def test_historical_receipt_import_remains_independent_of_review_pause(prepared, settings):
    slot, row, _, proof, _, _ = prepared
    assert consumer.receipts.reconcile(row.pk, consumer.old.REQUEST_ID, "synthetic") == "prepared"
    settings.MESSAGE_REVIEW_SOURCE_V2_ENABLED = False
    consumer.old.deliver(proof)
    assert consumer.receipts.reconcile(row.pk, consumer.old.REQUEST_ID, "synthetic") == "delivered"
    slot.refresh_from_db()
    assert slot.expectation.state == "delivered"


@pytest.mark.parametrize("status", ["created", "pending", "unknown", "review"])
def test_current_unresolved_money_blocks_review_even_with_collection_receipt(
    campaign, monkeypatch, status
):
    slot, row, _ = consumer.due(campaign, monkeypatch)
    stay = campaign[1]
    stay.attempts.create(
        merchant_transaction_id="synthetic-unresolved",
        amount=stay.amount_sar,
        currency="SAR",
        status=status,
    )
    with transaction.atomic(), pytest.raises(ValueError):
        review.facts_for_row(row)
    assert not slot.expectation_id
