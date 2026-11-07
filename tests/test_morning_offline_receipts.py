"""Synthetic manual morning receipts, no real bank/gateway/message calls."""

from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest
from django.core.exceptions import PermissionDenied, ValidationError
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from apps.notifications.models import AuditLog
from apps.payments.morning_payments import public_url, verify_payment
from apps.reservations.models import MorningOfflineReceipt
from apps.reservations.morning_receipts import record_receipt
from tests.test_hyperpay import HyperPayStub
from tests.test_morning_stays import approved, create, property_and_remote, start_payment
from tests.test_morning_stays import isolated as isolated  # noqa: F401

pytestmark = pytest.mark.django_db


def values(stay):
    return dict(
        request=SimpleNamespace(user=stay.created_by, META={}),
        stay_id=stay.pk,
        amount=stay.amount_sar,
        method="bank_transfer",
        received_at=timezone.now() - timedelta(minutes=5),
        reference="synthetic-bank-123",
        note="بنك تجريبي",
        confirmed=True,
    )


@pytest.mark.parametrize("method", ["cash", "bank_transfer"])
def test_manual_receipt_is_audited_idempotent_and_closes_payment_link(method):
    stay = create()
    data = values(stay) | {"method": method}
    receipt = record_receipt(**data)
    assert record_receipt(**data).pk == receipt.pk
    stay.refresh_from_db()
    assert stay.status == "paid" and stay.paid_at == data["received_at"]
    assert receipt.recorded_by == stay.created_by and receipt.amount == stay.amount_sar
    assert AuditLog.objects.filter(action="morning_stay.offline_received").count() == 1
    assert not stay.attempts.exists() and not stay.communication_events.exists()
    from urllib.parse import urlsplit

    page = Client().get(urlsplit(public_url(stay)).path)
    assert not page.context["payable"]
    with pytest.raises(ValidationError):
        record_receipt(**(data | {"method": "other"}))


@pytest.mark.parametrize("status", ["created", "pending", "unknown", "review", "succeeded"])
def test_unresolved_or_successful_online_payment_blocks_manual_receipt(status):
    p, remote = property_and_remote()
    stay = create(p, remote)
    attempt = start_payment(stay, remote, HyperPayStub())
    attempt.status = status
    attempt.save()
    with pytest.raises(ValidationError):
        record_receipt(**values(stay))
    assert not MorningOfflineReceipt.objects.exists()


@pytest.mark.parametrize(
    "changes",
    [
        {"amount": Decimal("499")},
        {"amount": Decimal("NaN")},
        {"confirmed": False},
        {"method": "card"},
        {"received_at": None},
    ],
)
def test_invalid_receipt_has_no_financial_effect(changes):
    stay = create()
    with pytest.raises(ValidationError):
        record_receipt(**(values(stay) | changes))
    assert not MorningOfflineReceipt.objects.exists()
    stay.refresh_from_db()
    assert not stay.paid_at


def test_cancelled_and_future_date_are_blocked():
    stay = create()
    with pytest.raises(ValidationError):
        record_receipt(**(values(stay) | {"received_at": timezone.now() + timedelta(days=1)}))
    stay.status = "cancelled"
    stay.save()
    with pytest.raises(ValidationError):
        record_receipt(**values(stay))


@pytest.mark.parametrize("late_success", [False, True])
def test_late_gateway_result_cannot_erase_manual_payment(late_success):
    p, remote = property_and_remote()
    stay = create(p, remote)
    gateway = HyperPayStub()
    attempt = start_payment(stay, remote, gateway)
    attempt.status = "failed"
    attempt.save()
    receipt = record_receipt(**values(stay))
    gateway.payment_document = approved(attempt)
    if not late_success:
        gateway.payment_document["result"]["code"] = "800.100.100"
    verify_payment(attempt.pk, client=gateway)
    stay.refresh_from_db()
    assert stay.paid_at == receipt.received_at
    assert stay.status == ("review" if late_success else "paid")


def test_page_has_collapsed_billing_and_explicit_receipt_confirmation():
    stay = create()
    client = Client()
    client.force_login(stay.created_by)
    url = reverse("notifications:morning_detail", args=[stay.pk])
    page = client.get(url)
    assert b'<details id="morning-billing">' in page.content
    assert b'name="confirmed"' in page.content
    data = {k: v for k, v in values(stay).items() if k not in {"request", "stay_id"}}
    data["action"] = "record_offline"
    assert client.post(url, data | {"amount": "1"}).status_code == 200
    assert not MorningOfflineReceipt.objects.exists()
    assert client.post(url, data).status_code == 302
    assert MorningOfflineReceipt.objects.count() == 1


def test_unprivileged_user_cannot_record_payment(django_user_model):
    stay = create()
    user = django_user_model.objects.create_user(username="unprivileged")
    client = Client()
    client.force_login(user)
    assert (
        client.post(
            reverse("notifications:morning_detail", args=[stay.pk]), {"action": "record_offline"}
        ).status_code
        == 403
    )
    with pytest.raises(PermissionDenied):
        record_receipt(**(values(stay) | {"request": SimpleNamespace(user=user, session={})}))
    assert not MorningOfflineReceipt.objects.exists()


def test_enabled_manager_can_record_and_csrf_is_required():
    from apps.accounts.models import OperationsManager
    from tests.test_operations_users import MANAGER, login_manager

    stay = create()
    OperationsManager.objects.create(email=MANAGER)
    user, client = login_manager()
    url = reverse("notifications:morning_detail", args=[stay.pk])
    data = {k: v for k, v in values(stay).items() if k not in {"request", "stay_id"}}
    data["action"] = "record_offline"
    protected = Client(enforce_csrf_checks=True)
    protected.cookies = client.cookies
    assert protected.post(url, data).status_code == 403
    assert client.post(url, data).status_code == 302
    assert MorningOfflineReceipt.objects.get(stay=stay).recorded_by == user


@pytest.mark.django_db(transaction=True)
def test_parallel_receipts_are_serialized():
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    from django.db import close_old_connections, connection

    if connection.vendor != "postgresql":
        pytest.skip("Concurrent receipt test requires PostgreSQL row locks")
    stay = create()
    data = values(stay)
    barrier = Barrier(2)

    def submit():
        close_old_connections()
        try:
            barrier.wait(timeout=10)
            return record_receipt(**data).pk
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as pool:
        ids = list(pool.map(lambda _: submit(), range(2)))
    assert ids[0] == ids[1]
    assert MorningOfflineReceipt.objects.count() == 1
