"""Fictional cash/bank records only; never bank, Hostaway or WhatsApp calls."""

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from threading import Barrier
from unittest.mock import Mock, patch

import pytest
from django.contrib.auth.models import AnonymousUser
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import close_old_connections, connection
from django.test import Client, RequestFactory
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import OperationsManager
from apps.notifications.models import AuditLog
from apps.payments.hyperpay.exceptions import HyperPayCheckoutError, HyperPayRefundError
from apps.payments.hyperpay.refunds import HyperPayRefundService
from apps.payments.models import OfflinePaymentReceipt, PaymentAttempt
from apps.payments.offline_receipts import receipt_snapshot, record_offline_receipt
from apps.payments.payment_links import issue_request
from apps.reservations.models import RefundObligation
from apps.reservations.services.automatic_modifications import _submit_automatic_refund
from apps.reservations.services.owner_settlement import settlement_for
from apps.reservations.services.refunds import successful_original_payment_amount
from tests.test_hyperpay import HYPERPAY_SETTINGS
from tests.test_manual_booking_operations_phase61 import owner
from tests.test_operations_users import login_manager
from tests.test_reservation_payment_links import checkout, manual_stay, page_url

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def configured(settings):
    settings.MANUAL_PAYMENT_RECEIPTS_ENABLED = True
    settings.RESERVATION_PAYMENT_LINKS_ENABLED = True
    for key, value in HYPERPAY_SETTINGS.items():
        setattr(settings, key, value)
    with patch("httpx.Client.request", side_effect=AssertionError("unexpected provider request")):
        yield


def actor_request(actor=None):
    request = RequestFactory().post("/admin/operations/bookings/")
    request.user = actor or owner()
    request.session = {}
    return request


def values(stay, **overrides):
    return {
        "amount": stay.total_price,
        "currency": stay.currency,
        "method": "bank_transfer",
        "reference": "FICTIONAL-RECEIPT-1",
        "received_at": timezone.now() - timedelta(minutes=2),
        "snapshot_digest": receipt_snapshot(stay),
        "confirmed": True,
        **overrides,
    }


@pytest.mark.parametrize(
    "method,currency", [("cash", "SAR"), ("bank_transfer", "SAR"), ("bank_transfer", "MAD")]
)
def test_receipt_is_audited_full_and_idempotent_without_provider_calls(method, currency):
    stay = manual_stay(currency=currency)
    payment_link = issue_request(stay.pk)
    request = actor_request()
    data = values(stay, method=method)
    receipt = record_offline_receipt(request=request, reservation_id=stay.pk, **data)
    assert record_offline_receipt(request=request, reservation_id=stay.pk, **data).pk == receipt.pk
    stay.refresh_from_db()
    assert stay.payment_status == "paid"
    assert settlement_for(stay, stay.total_price).paid == stay.total_price
    payment = receipt.payment_attempt
    assert payment.provider == "manual" and payment.payment_brand == method
    assert payment.provider_checkout_id is None
    assert payment.provider_payment_id.startswith("manual:")
    assert not PaymentAttempt.objects.filter(provider="hyperpay").exists()
    assert AuditLog.objects.filter(action="payment.offline_received").count() == 1
    assert receipt.recorded_by_id == request.user.pk
    payment_link.refresh_from_db()
    assert payment_link.status == "revoked"
    assert b'name="billing_city"' not in Client().get(page_url(payment_link)).content
    with pytest.raises(HyperPayCheckoutError):
        issue_request(stay.pk)


def test_existing_direct_booking_link_does_not_require_an_admin_draft():
    stay = manual_stay()
    stay.booking_intent.quote.manual_booking_draft.delete()
    assert issue_request(stay.pk).reservation_id == stay.pk
    stay.source_type = "external_channel"
    stay.save()
    with pytest.raises(HyperPayCheckoutError):
        issue_request(stay.pk)


@pytest.mark.parametrize(
    "status",
    ["created", "pending", "unknown", "review", "succeeded", "partially_refunded", "refunded"],
)
def test_any_unresolved_or_previous_collection_blocks_manual_full_payment(status):
    stay = manual_stay()
    PaymentAttempt.objects.create(
        booking_intent=stay.booking_intent,
        provider="hyperpay",
        currency="SAR",
        amount=stay.total_price,
        status=status,
        idempotency_key="another-attempt",
    )
    with pytest.raises(ValidationError):
        record_offline_receipt(request=actor_request(), reservation_id=stay.pk, **values(stay))
    assert not OfflinePaymentReceipt.objects.exists()


@pytest.mark.parametrize(
    "change",
    [
        {"amount": "1"},
        {"currency": "USD"},
        {"confirmed": False},
        {"method": "card"},
        {"reference": ""},
        {"snapshot_digest": "stale"},
    ],
)
def test_invalid_or_changed_confirmation_never_marks_paid(change):
    stay = manual_stay()
    with pytest.raises(ValidationError):
        record_offline_receipt(
            request=actor_request(), reservation_id=stay.pk, **values(stay, **change)
        )
    stay.refresh_from_db()
    assert stay.payment_status == "unpaid" and not OfflinePaymentReceipt.objects.exists()


def test_cash_can_be_recorded_for_imported_booking_without_fabricating_an_intent():
    stay = manual_stay()
    stay.booking_intent = None
    stay.source_type = "hostaway_manual"
    stay.save()
    receipt = record_offline_receipt(
        request=actor_request(), reservation_id=stay.pk, **values(stay)
    )
    assert receipt.payment_attempt_id is None
    assert PaymentAttempt.objects.count() == 0
    assert settlement_for(stay, stay.total_price).paid == stay.total_price
    assert successful_original_payment_amount(stay) == stay.total_price


def test_manual_refund_stays_manual_and_never_contacts_a_card_provider():
    stay = manual_stay()
    record_offline_receipt(request=actor_request(), reservation_id=stay.pk, **values(stay))
    refund = RefundObligation.objects.create(
        reservation=stay,
        reason="cancellation",
        amount=stay.total_price,
        currency=stay.currency,
    )
    gateway = Mock()
    assert _submit_automatic_refund(refund, service=gateway) == "manual_refund_required"
    gateway.submit.assert_not_called()
    with pytest.raises(HyperPayRefundError, match="manual_refund_required"):
        HyperPayRefundService._prepare(refund.pk)


def test_service_rejects_guest_and_disabled_feature(settings):
    stay = manual_stay()
    request = actor_request()
    operator = request.user
    request.user = AnonymousUser()
    with pytest.raises(PermissionDenied):
        record_offline_receipt(request=request, reservation_id=stay.pk, **values(stay))
    request.user = operator
    settings.MANUAL_PAYMENT_RECEIPTS_ENABLED = False
    with pytest.raises(ValidationError):
        record_offline_receipt(request=request, reservation_id=stay.pk, **values(stay))


def test_real_operations_page_records_receipt_and_rejects_missing_confirmation():
    stay = manual_stay()
    client = Client()
    client.force_login(owner())
    url = reverse("notifications:booking_detail", args=[stay.pk])
    assert "تسجيل الحجز مدفوعًا" in client.get(url).content.decode()
    data = values(stay) | {"action": "record_offline_payment"}
    data.pop("confirmed")
    assert client.post(url, data).status_code == 200
    assert not OfflinePaymentReceipt.objects.exists()
    assert client.post(url, data | {"confirmed": "on"}).status_code == 302
    assert OfflinePaymentReceipt.objects.count() == 1


def test_approved_google_manager_can_record_through_operations_page(settings):
    settings.OPERATIONS_OWNER_ENFORCEMENT_ENABLED = True
    settings.OPERATIONS_OWNER_EMAIL = "saeed@luxurysmartapartments.com"
    OperationsManager.objects.create(email="synthetic.lsa.manager@gmail.com")
    user, client = login_manager()
    stay = manual_stay()
    data = values(stay) | {"action": "record_offline_payment", "confirmed": "on"}
    response = client.post(reverse("notifications:booking_detail", args=[stay.pk]), data)
    assert response.status_code == 302
    assert OfflinePaymentReceipt.objects.get().recorded_by_id == user.pk


@pytest.mark.django_db(transaction=True)
def test_postgres_parallel_cash_confirmation_creates_one_receipt():
    if connection.vendor != "postgresql":
        pytest.skip("requires real PostgreSQL row locks in CI")
    stay, operator = manual_stay(), owner()
    data, barrier = values(stay), Barrier(2)

    def record():
        close_old_connections()
        try:
            barrier.wait(timeout=10)
            return record_offline_receipt(
                request=actor_request(operator), reservation_id=stay.pk, **data
            ).pk
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(record), pool.submit(record)]
        ids = [future.result(timeout=20) for future in futures]
    assert ids[0] == ids[1]
    assert OfflinePaymentReceipt.objects.count() == PaymentAttempt.objects.count() == 1


@pytest.mark.django_db(transaction=True)
def test_postgres_cash_and_online_checkout_cannot_both_claim_the_booking():
    if connection.vendor != "postgresql":
        pytest.skip("requires real PostgreSQL row locks in CI")
    stay, operator = manual_stay(), owner()
    link = issue_request(stay.pk)
    data, barrier = values(stay), Barrier(2)

    def claim(kind):
        close_old_connections()
        try:
            barrier.wait(timeout=10)
            if kind == "cash":
                record_offline_receipt(
                    request=actor_request(operator), reservation_id=stay.pk, **data
                )
            else:
                checkout(link)
            return kind
        except (ValidationError, HyperPayCheckoutError):
            return "blocked"
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(claim, "cash"), pool.submit(claim, "online")]
        results = [future.result(timeout=20) for future in futures]
    assert results.count("blocked") == 1
    assert PaymentAttempt.objects.count() == 1
    if "cash" in results:
        assert OfflinePaymentReceipt.objects.count() == 1
        assert PaymentAttempt.objects.get().provider == "manual"
    else:
        assert not OfflinePaymentReceipt.objects.exists()
        assert PaymentAttempt.objects.get().provider == "hyperpay"


@pytest.mark.parametrize("legacy_first", [True, False])
def test_reference_is_shared_between_old_and_new_receipt_workflows(
    legacy_first, settings, monkeypatch
):
    from apps.loyalty.settlements import record_manual_receipt
    from tests.test_loyalty_settlement_integration import manual_booking, user

    settings.LOYALTY_ENABLED = True
    settings.LOYALTY_LAUNCH_AT = "2026-10-01T00:00:00+03:00"
    monkeypatch.setattr(timezone, "now", lambda: datetime(2026, 10, 10, 13, tzinfo=UTC))
    old = manual_booking(user("legacy-ref"))
    new, operator = manual_stay(), owner()

    def legacy():
        return record_manual_receipt(
            operator,
            old.pk,
            value=old.total_price,
            received_at=timezone.now(),
            reference="Same-Receipt",
            lodging_only=True,
        )

    def modern():
        return record_offline_receipt(
            request=actor_request(operator),
            reservation_id=new.pk,
            **values(new, reference="same-receipt"),
        )

    first, second = (legacy, modern) if legacy_first else (modern, legacy)
    first()
    with pytest.raises(ValidationError):
        second()
    assert PaymentAttempt.objects.filter(provider="manual").count() == 1


def test_admin_override_lodging_receipt_awards_points_once(settings, monkeypatch):
    from apps.loyalty.services import balance, reconcile
    from tests.test_loyalty_settlement_integration import join, manual_booking, user

    settings.LOYALTY_ENABLED = True
    settings.LOYALTY_LAUNCH_AT = "2026-10-01T00:00:00+03:00"
    monkeypatch.setattr(timezone, "now", lambda: datetime(2026, 10, 10, 13, tzinfo=UTC))
    guest = user("new-manual-price")
    member = join(guest)
    stay = manual_booking(guest)
    quote = stay.booking_intent.quote
    quote.total_price = 1000
    quote.components = [{"type": "baseRate", "total": "1000.00", "included_in_total": True}]
    quote.save()
    draft = quote.manual_booking_draft
    draft.system_total_price = 1000
    draft.final_total_price = stay.total_price
    draft.save()
    record_offline_receipt(
        request=actor_request(),
        reservation_id=stay.pk,
        **values(stay, amount="750.00", lodging_only=True),
    )
    reconcile(stay.pk)
    reconcile(stay.pk)
    assert balance(member) == 22.50


def test_repeated_receipt_cannot_silently_change_received_time():
    stay, request = manual_stay(), actor_request()
    data = values(stay)
    record_offline_receipt(request=request, reservation_id=stay.pk, **data)
    data["received_at"] -= timedelta(minutes=1)
    with pytest.raises(ValidationError):
        record_offline_receipt(request=request, reservation_id=stay.pk, **data)


def test_migration_backfills_existing_manual_reference_without_rewriting_payment():
    from importlib import import_module
    from types import SimpleNamespace

    from django.apps import apps

    from apps.payments.manual_references import reference_digest
    from apps.payments.models import ManualReceiptReference

    stay = manual_stay()
    payment = PaymentAttempt.objects.create(
        booking_intent=stay.booking_intent,
        provider="manual",
        provider_reference="Historical-Bank-1",
        amount=stay.total_price,
        currency=stay.currency,
        status="succeeded",
        verified_at=timezone.now(),
        idempotency_key="historical-receipt",
    )
    migrate = import_module("apps.payments.migrations.0012_manualreceiptreference")
    migrate.backfill_references(apps, SimpleNamespace(connection=connection))
    claim = ManualReceiptReference.objects.get(digest=reference_digest("HISTORICAL-BANK-1"))
    assert claim.payment_attempt_id == payment.pk and claim.reservation_id == stay.pk
    payment.refresh_from_db()
    assert payment.provider_reference == "Historical-Bank-1"
    migrate.backfill_references(apps, SimpleNamespace(connection=connection))
    assert ManualReceiptReference.objects.count() == 1
