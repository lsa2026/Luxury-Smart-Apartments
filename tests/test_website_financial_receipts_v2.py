from datetime import timedelta

import pytest
from django.utils import timezone

from apps.reservations import message_source_relay as relay
from apps.reservations import website_financial_receipts_v2 as receipts
from apps.reservations.message_receipt_models import WebsiteMessageExpectation
from tests import test_website_financial_source_v2 as source_tests

record, prepared = source_tests.record, source_tests.prepared
pytestmark = pytest.mark.django_db


@pytest.fixture
def proof(prepared):
    scenario, stay, _, delivery, _ = prepared
    delivery.state = "waiting_core"
    delivery.save(update_fields=["state"])
    row = relay.WebsiteMessageRelay.objects.create(
        scenario=scenario,
        financial=False,
        stay_id=stay.pk,
        event_id=delivery.pk,
        message_type="payment_link",
        revision=str(stay.communication_revision),
        job_sha256="a" * 64,
        source_occurred_at=delivery.created_at,
        not_before=delivery.created_at,
        expires_at=stay.ends_at,
        state="accepted",
    )
    phone = receipts.source.normalize_phone_number(stay.guest_phone)
    recipient = (
        "site01:"
        + receipts.source.salted_hmac(
            "website-source-recipient.v1", f"{scenario}:{stay.pk}:{phone}", algorithm="sha256"
        ).hexdigest()
    )
    expectation = WebsiteMessageExpectation.objects.create(
        request_id="n8n20-" + "a" * 64,
        relay=row,
        prepare_ref="b" * 64,
        prepared_at_epoch=int(timezone.now().timestamp()),
        render_policy_ref="source-v2-synthetic",
        attempts_claimed=1,
        state="accepted",
        provider_message_id="synthetic-ycloud-id",
        identity={
            "source_family": f"lsa{scenario}_payment_link",
            "source_event_key": f"website:{scenario}:payment_link:{delivery.pk}",
            "reservation_revision": str(stay.communication_revision),
            "recipient_ref": recipient,
        },
    )
    return stay, delivery, expectation


def test_verified_message_receipt_releases_original_resource_without_money_action(proof, settings):
    stay, delivery, expected = proof
    settings.MESSAGE_SOURCE_V2_ENABLED = (
        False  # Historical receipt is independent of future dispatch.
    )
    assert receipts.project_payment_link_receipt(expected)
    delivery.refresh_from_db()
    assert delivery.state == "accepted" and delivery.provider_id == "synthetic-ycloud-id"
    expected.state = "delivered"
    assert receipts.project_payment_link_receipt(expected)
    expected.state = "accepted"
    assert receipts.project_payment_link_receipt(expected)
    delivery.refresh_from_db()
    assert delivery.state == "delivered"
    stay.refresh_from_db()
    assert stay.paid_at is None and stay.status == "open" and stay.attempts.count() == 0


@pytest.mark.parametrize("change", ["phone", "revision", "window", "uncertain", "foreign_provider"])
def test_historical_receipt_does_not_release_changed_or_ambiguous_resource(proof, change):
    stay, delivery, expected = proof
    if change == "phone":
        type(stay).objects.filter(pk=stay.pk).update(guest_phone="+16502539999")
    elif change == "revision":
        type(stay).objects.filter(pk=stay.pk).update(communication_revision=100)
    elif change == "window":
        type(stay).objects.filter(pk=stay.pk).update(ends_at=stay.ends_at + timedelta(hours=1))
    elif change == "uncertain":
        type(delivery).objects.filter(pk=delivery.pk).update(state="uncertain")
    else:
        type(delivery).objects.filter(pk=delivery.pk).update(provider_id="other-provider")
    assert not receipts.project_payment_link_receipt(expected)
    delivery.refresh_from_db()
    assert delivery.state in {"waiting_core", "uncertain"}


def test_unknown_central_attempt_without_source_provider_id_blocks_fresh_uuid(proof):
    from apps.reservations.website_financial_source_v2 import payment_link_source

    stay, old_delivery, expected = proof
    expected.state = "uncertain"
    expected.provider_message_id = ""
    expected.save(update_fields=["state", "provider_message_id"])
    new = stay.payment_link_deliveries.create(fingerprint=old_delivery.fingerprint)
    result = payment_link_source(new.pk, expected.relay.scenario)
    assert result["available"] is False
    assert result["reason"] == "prior_delivery_requires_reconciliation"
