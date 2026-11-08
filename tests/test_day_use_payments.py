"""Independent checkout and verified settlement; disposable provider responses only."""
# ruff: noqa: F811

from unittest.mock import Mock
from uuid import uuid4

import pytest
from django.urls import reverse
from django.utils import timezone

from apps.payments import day_use_payments as payments
from apps.payments.hyperpay.exceptions import HyperPayCheckoutError
from apps.payments.views import _hyperpay_return_token
from apps.reservations import day_use08_communications as communications
from apps.reservations import day_use08_payments as financial
from apps.reservations import day_use_loyalty
from apps.reservations.models import DayUsePaymentLinkDelivery
from tests.test_day_use08 import rig as rig
from tests.test_day_use08 import snapshot, stay
from tests.test_hyperpay import HYPERPAY_SETTINGS, HyperPayStub
from tests.test_morning_stays import approved, consent
from tests.test_reservation_payment_links import billing

pytestmark = pytest.mark.django_db


@pytest.fixture
def ready(rig, settings, monkeypatch):
    for key, value in HYPERPAY_SETTINGS.items():
        setattr(settings, key, value)
    settings.HYPERPAY_ENVIRONMENT = "production"
    s = stay(rig)
    settings.DAY_USE08_ENABLED = True
    monkeypatch.setattr(communications, "queue_stay_event", lambda _: None)
    monkeypatch.setattr(financial, "queue_event", lambda _: None)
    monkeypatch.setattr(day_use_loyalty, "core_delivered", lambda *args, **kwargs: True)
    s.availability_evidence["hostaway_day_use"] = {
        "state": "confirmed",
        "reservation_id": 99808,
        "arrival_date": s.inventory_arrival_date.isoformat(),
        "departure_date": s.inventory_departure_date.isoformat(),
    }
    s.save()
    rig.remote.guest_stay_page.return_value = [
        dict(
            id=99808,
            listingMapId=97008,
            status="new",
            source="LSA_DayUse",
            arrivalDate=s.inventory_arrival_date.isoformat(),
            departureDate=s.inventory_departure_date.isoformat(),
        )
    ]
    rig.remote.get_reservation.return_value = snapshot(rig)
    DayUsePaymentLinkDelivery.objects.create(
        pk=uuid4(), stay=s, state="accepted", fingerprint="test"
    )
    return rig, s, HyperPayStub()


def start(ready, acceptance=None):
    rig, s, gateway = ready
    return payments.create_checkout(
        s.pk,
        billing_data=billing(),
        acceptance=acceptance or consent(),
        client=gateway,
        hostaway=rig.remote,
    )


def test_real_guest_acceptance_is_required_before_provider_checkout(ready):
    with pytest.raises(HyperPayCheckoutError, match="acceptance"):
        start(ready, {"consents": {}})
    assert ready[2].checkout_calls == 0


def test_checkout_amount_and_idempotency_and_verified_paid_outbox(ready):
    _, s, gateway = ready
    attempt = start(ready)
    assert gateway.payload["amount"] == "740.93"
    assert start(ready).pk == attempt.pk and gateway.checkout_calls == 1
    gateway.payment_document = approved(attempt, id="day-use-capture-001")
    payments.verify_payment(attempt.pk, client=gateway)
    s.refresh_from_db()
    assert s.status == "paid" and s.paid_at
    assert s.payment_events.count() == 1 and s.communication_events.count() == 1
    payments.verify_payment(attempt.pk, client=gateway)
    assert s.payment_events.count() == 1


@pytest.mark.parametrize(
    "changes",
    [
        {"amount": "740.94"},
        {"currency": "USD"},
        {"merchantTransactionId": "another-stay"},
        {"id": ""},
    ],
)
def test_gateway_return_cannot_mark_paid_on_mismatched_authoritative_result(ready, changes):
    _, s, gateway = ready
    attempt = start(ready)
    gateway.payment_document = approved(attempt, **changes)
    payments.verify_payment(attempt.pk, client=gateway)
    s.refresh_from_db()
    assert s.status == "review" and not s.paid_at and not s.payment_events.exists()


def test_core_receipts_are_rechecked_at_checkout(ready, monkeypatch):
    monkeypatch.setattr(day_use_loyalty, "core_delivered", lambda *a, **k: False)
    with pytest.raises(HyperPayCheckoutError, match="core_not_delivered"):
        start(ready)
    assert ready[2].checkout_calls == 0


@pytest.mark.parametrize(
    "code,status",
    [
        ("000.200.100", "pending"),
        ("800.100.152", "failed"),
        ("100.396.101", "cancelled"),
        ("000.400.100", "review"),
        ("unrecognized", "unknown"),
    ],
)
def test_non_successful_payment_never_creates_paid_notice_or_loyalty(ready, code, status):
    _, s, gateway = ready
    attempt = start(ready)
    gateway.payment_document = approved(attempt, result={"code": code})
    result = payments.verify_payment(attempt.pk, client=gateway)
    assert result.status == status
    s.refresh_from_db()
    assert not s.paid_at and not s.payment_events.exists()


def test_signed_guest_page_and_result_use_independent_ledger(ready, client, monkeypatch):
    _, s, gateway = ready
    url = payments.public_url(s).replace("http://testserver", "").replace("https://testserver", "")
    assert client.get(url).status_code == 200 and gateway.checkout_calls == 0
    attempt = start(ready)
    result = reverse("payments:day_use_result", args=[attempt.pk])
    assert client.get(result).status_code == 404
    gateway.payment_document = approved(attempt, id="day-use-page-capture")
    monkeypatch.setattr(
        "apps.payments.day_use_views.verify_payment",
        lambda pk: payments.verify_payment(pk, client=gateway),
    )
    assert client.get(result, {"return_token": _hyperpay_return_token(attempt)}).status_code == 200


def test_phone_correction_is_blocked_after_link_release(ready):
    from apps.reservations.day_use_phone import correct_phone
    from apps.reservations.day_use_stays import DayUseStayError

    rig, s, _ = ready
    with pytest.raises(DayUseStayError, match="مراجعة"):
        correct_phone(
            s.pk, "+16502530001", expected_updated_at=s.updated_at.isoformat(), client=rig.remote
        )
    rig.remote.update_day_use_phone.assert_not_called()


def test_phone_provider_uncertainty_prevents_replay(rig, settings, monkeypatch):
    from apps.reservations.day_use_phone import correct_phone
    from apps.reservations.day_use_stays import DayUseStayError

    s = stay(rig)
    settings.DAY_USE08_ENABLED = True
    monkeypatch.setattr(communications, "queue_stay_event", lambda _: None)
    s.availability_evidence["hostaway_day_use"] = {"state": "confirmed", "reservation_id": 99808}
    s.save()
    rig.remote.update_day_use_phone.side_effect = TimeoutError
    with pytest.raises(DayUseStayError, match="غير محسومة"):
        correct_phone(
            s.pk, "+16502530001", expected_updated_at=s.updated_at.isoformat(), client=rig.remote
        )
    s.refresh_from_db()
    assert s.guest_phone == "+16502530000" and not communications.admitted(s)
    with pytest.raises(DayUseStayError):
        correct_phone(
            s.pk, "+16502530001", expected_updated_at=s.updated_at.isoformat(), client=rig.remote
        )
    assert rig.remote.update_day_use_phone.call_count == 1


def morning_attempt_for(rig, s):
    from apps.payments.models import MorningPaymentAttempt
    from apps.reservations.models import MorningStay

    other = MorningStay.objects.create(
        request_id=uuid4(),
        property=rig.p,
        starts_at=rig.start.replace(hour=1),
        ends_at=rig.start.replace(hour=11),
        guest_name="Synthetic Other Guest",
        guest_phone="+16502530001",
        guest_email="other@example.test",
        guests=1,
        amount_sar=s.amount_sar,
        environment="production",
        status="processing",
        expires_at=rig.start,
        availability_checked_at=timezone.now(),
        created_by=rig.actor,
    )
    return MorningPaymentAttempt.objects.create(
        stay=other,
        merchant_transaction_id="other-ledger",
        amount=s.amount_sar,
        status="pending",
        provider_checkout_id="synthetic-other-checkout",
    )


def test_one_provider_payment_id_cannot_settle_morning_and_day_use(ready):
    rig, s, gateway = ready
    attempt = start(ready)
    other = morning_attempt_for(rig, s)
    other.provider_payment_id = "already-used-money"
    other.status = "succeeded"
    other.verified_at = timezone.now()
    other.save()
    gateway.payment_document = approved(attempt, id="already-used-money")
    result = payments.verify_payment(attempt.pk, client=gateway)
    assert result.status == "review" and result.failure_code == "payment_id_already_recorded"
    s.refresh_from_db()
    assert not s.paid_at and not s.payment_events.exists()


@pytest.mark.django_db(transaction=True)
def test_concurrent_cross_ledger_provider_proofs_have_one_winner(ready):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    from django.db import close_old_connections, connection

    from apps.payments.morning_payments import verify_payment as verify_morning

    if connection.vendor != "postgresql":
        pytest.skip("Cross-ledger provider ID serialization requires PostgreSQL")
    rig, s, _ = ready
    day_attempt = start(ready)
    other = morning_attempt_for(rig, s)
    barrier = Barrier(2)

    def worker(pair):
        attempt, verify = pair
        close_old_connections()
        provider = Mock()

        def result(checkout_id):
            barrier.wait(timeout=10)
            return approved(attempt, id="same-cross-ledger-capture")

        provider.get_checkout_payment.side_effect = result
        try:
            return verify(attempt.pk, client=provider).status
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(
            pool.map(worker, [(day_attempt, payments.verify_payment), (other, verify_morning)])
        )
    assert sorted(results) == ["review", "succeeded"]
