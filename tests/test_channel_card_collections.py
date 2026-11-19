"""Synthetic Booking VCC orders; every real provider request is forbidden."""

import hashlib
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from decimal import Decimal
from threading import Barrier
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.db import close_old_connections, connection, transaction
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from apps.core.models import MarketingEventReceipt
from apps.notifications.models import AuditLog, GuestInvoice, WhatsAppDelivery
from apps.payments import channel_collections as service
from apps.payments.channel_forms import ChannelCollectionForm
from apps.payments.collection_identity import payment_id_conflict
from apps.payments.hyperpay.exceptions import HyperPayConnectionError, HyperPayError
from apps.payments.models import ChannelCardCollection, PaymentAttempt
from apps.payments.views import _hyperpay_return_token
from apps.properties.models import Property
from apps.reservations.models import BookingIntent, Reservation
from tests.test_hyperpay import HYPERPAY_SETTINGS

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def isolated(settings, monkeypatch):
    for key, value in HYPERPAY_SETTINGS.items():
        setattr(settings, key, value)
    settings.HYPERPAY_ENVIRONMENT = "production"
    settings.BOOKING_VCC_COLLECTION_ENABLED = True
    settings.BOOKING_VCC_APPROVAL_REFERENCE = "synthetic-only-merchant-proof"
    settings.BOOKING_VCC_APPROVED_ENVIRONMENT = "production"
    settings.BOOKING_VCC_APPROVED_ENTITY_FINGERPRINT = hashlib.sha256(
        settings.HYPERPAY_ENTITY_ID.encode()
    ).hexdigest()
    settings.GOOGLE_INTEGRATIONS_ENABLED = True
    settings.GOOGLE_TAG_MANAGER_ENABLED = True
    settings.GOOGLE_TAG_MANAGER_CONTAINER_ID = "GTM-SYNTHETIC"
    settings.OPERATIONS_OWNER_ENFORCEMENT_ENABLED = True
    settings.OPERATIONS_OWNER_EMAIL = "owner@example.invalid"
    settings.MANUAL_PAYMENT_RECEIPTS_ENABLED = True
    cache.clear()
    monkeypatch.setattr(
        "httpx.Client.request", Mock(side_effect=AssertionError("live network forbidden"))
    )


@pytest.fixture
def rig(settings):
    owner = get_user_model().objects.create_superuser(
        username="owner", email=settings.OPERATIONS_OWNER_EMAIL, password="synthetic-test-only"
    )
    place = Property.objects.create(
        hostaway_listing_id=920001,
        hostaway_listing_map_id=920001,
        slug="synthetic-vcc-suite",
        name_ar="شقة اختبار",
        name_en="Synthetic suite",
        city="Riyadh",
    )
    remote = Mock()
    remote.get_reservation.return_value = SimpleNamespace(
        reservation_id=930001,
        listing_map_id=place.hostaway_listing_map_id,
        channel_id=99,
        source="Booking.com",
        status="confirmed",
        check_in=timezone.localdate() - timedelta(days=2),
        check_out=timezone.localdate() - timedelta(days=1),
        currency="SAR",
        total_price=Decimal("900.00"),
    )
    gateway = Mock()
    gateway.create_checkout.return_value = {
        "id": "synthetic-checkout-1234",
        "integrity": "sha384-YWJj",
        "result": {"code": "000.200.100"},
    }
    return SimpleNamespace(owner=owner, place=place, remote=remote, gateway=gateway)


def inputs(**changes):
    values = {
        "hostaway_id": "930001",
        "amount": "670.24",
        "currency": "SAR",
        "available_on": timezone.localdate(),
        "charge_before": timezone.localdate() + timedelta(days=30),
        "balance_confirmed": True,
        "payer_confirmed": True,
        "guest_first_name": "Corporate",
        "guest_last_name": "Billing",
        "guest_email": "billing@example.invalid",
        "billing_street1": "Synthetic Billing Street",
        "billing_city": "Riyadh",
        "billing_state": "Riyadh",
        "billing_country": "SA",
        "billing_postcode": "12345",
    }
    values.update(changes)
    return values


def draft(rig):
    return service.create_collection(inputs(), actor=rig.owner, hostaway=rig.remote)


def started(rig):
    order = draft(rig)
    return service.start_checkout(
        order.pk, actor=rig.owner, client=rig.gateway, hostaway=rig.remote
    )


def document(order, **changes):
    value = {
        "id": "synthetic-capture-1234",
        "amount": str(order.amount),
        "currency": order.currency,
        "merchantTransactionId": order.merchant_transaction_id,
        "entityId": "test-entity-id",
        "paymentType": "DB",
        "paymentBrand": "MASTER",
        "result": {"code": "000.000.000"},
    }
    value.update(changes)
    return value


def counts():
    return [
        m.objects.count()
        for m in (
            Reservation,
            BookingIntent,
            GuestInvoice,
            WhatsAppDelivery,
            MarketingEventReceipt,
            PaymentAttempt,
        )
    ]


def test_external_order_and_capture_do_not_create_stay_invoice_message_purchase_or_guest_consent(
    rig,
):
    before = counts()
    order = started(rig)
    rig.gateway.get_checkout_payment.return_value = document(order)
    result = service.reconcile(order.pk, actor=rig.owner, client=rig.gateway)
    assert result.status == "succeeded" and result.verified_at
    assert result.amount == Decimal("670.24") and result.currency == "SAR"
    assert result.reservation_id is None and result.property_id == rig.place.pk
    assert counts() == before
    assert rig.gateway.create_checkout.call_count == 1
    payload = rig.gateway.create_checkout.call_args.args[0]
    assert payload["amount"] == "670.24" and payload["currency"] == "SAR"
    assert payload["customer.email"] == "billing@example.invalid"
    assert not any(
        "moto" in key.lower() or "mit" in key.lower() or "3ds" in key.lower() for key in payload
    )
    assert "terms_accepted" not in result.payer and "card_number" not in result.payer
    assert set(result.payer) == set(ChannelCollectionForm()._billing_rule_names)
    assert AuditLog.objects.filter(
        action="channel_collection.verified", actor_user=rig.owner
    ).exists()


@pytest.mark.parametrize(
    "changes",
    [
        {"amount": "0"},
        {"amount": "-1"},
        {"amount": "670.241"},
        {"amount": "NaN"},
        {"currency": "EUR"},
        {"balance_confirmed": False},
        {"payer_confirmed": False},
        {"hostaway_id": "wrong"},
        {"card_number": "synthetic-card"},
        {"cvc": "synthetic-code"},
        {"guest_first_name": "4111111111111111"},
        {"guest_first_name": "٤١١١١١١١١١١١١١١١"},
        {"billing_state": "Riyadh Province"},
    ],
)
def test_bad_or_card_inputs_rejected_before_provider_io(rig, changes):
    with pytest.raises(ValidationError):
        service.create_collection(inputs(**changes), actor=rig.owner, hostaway=rig.remote)
    rig.remote.get_reservation.assert_not_called()
    assert ChannelCardCollection.objects.count() == 0


def test_payer_country_is_not_assumed_and_shared_address_rules_are_visible():
    form = ChannelCollectionForm()
    assert form.initial["billing_country"] == ""
    assert form.fields["billing_state"].widget.attrs["data-morning-pattern"] == "[a-zA-Z0-9.]{1,50}"
    assert "حامل البطاقة" in form.fields["guest_first_name"].label


@pytest.mark.parametrize("kind", ["source", "status", "listing", "identity"])
def test_wrong_external_booking_is_rejected(rig, kind):
    setattr(
        rig.remote.get_reservation.return_value,
        {
            "source": "source",
            "status": "status",
            "listing": "listing_map_id",
            "identity": "reservation_id",
        }[kind],
        {"source": "Airbnb", "status": "cancelled", "listing": 999999, "identity": 999999}[kind],
    )
    with pytest.raises(ValidationError):
        draft(rig)
    assert ChannelCardCollection.objects.count() == 0


def test_duplicate_create_returns_same_order_and_different_amount_is_refused(rig):
    first = draft(rig)
    assert draft(rig).pk == first.pk
    with pytest.raises(ValidationError):
        service.create_collection(inputs(amount="600"), actor=rig.owner, hostaway=rig.remote)
    assert ChannelCardCollection.objects.count() == 1


@pytest.mark.parametrize("gate", ["disabled", "reference", "entity", "environment"])
def test_closed_or_changed_merchant_approval_blocks_gateway_post(rig, settings, gate):
    order = draft(rig)
    if gate == "disabled":
        settings.BOOKING_VCC_COLLECTION_ENABLED = False
    elif gate == "reference":
        settings.BOOKING_VCC_APPROVAL_REFERENCE = ""
    elif gate == "entity":
        settings.HYPERPAY_ENTITY_ID = "different-merchant"
    else:
        settings.BOOKING_VCC_APPROVED_ENVIRONMENT = "test"
    with pytest.raises(ValidationError):
        service.start_checkout(order.pk, actor=rig.owner, client=rig.gateway, hostaway=rig.remote)
    rig.gateway.create_checkout.assert_not_called()
    order.refresh_from_db()
    assert order.status == "draft"


def test_stale_balance_requires_explicit_new_attestation(rig):
    order = draft(rig)
    ChannelCardCollection.objects.filter(pk=order.pk).update(
        balance_checked_at=timezone.now() - timedelta(minutes=11)
    )
    with pytest.raises(ValidationError):
        service.start_checkout(order.pk, actor=rig.owner, client=rig.gateway, hostaway=rig.remote)
    with pytest.raises(ValidationError):
        service.refresh_balance(order.pk, actor=rig.owner, hostaway=rig.remote)
    service.refresh_balance(order.pk, actor=rig.owner, balance_confirmed=True, hostaway=rig.remote)
    assert (
        service.start_checkout(
            order.pk, actor=rig.owner, client=rig.gateway, hostaway=rig.remote
        ).status
        == "pending"
    )


@pytest.mark.parametrize("field", ["available_on", "charge_before"])
def test_payment_outside_card_window_cannot_start(rig, field):
    order = draft(rig)
    value = (
        timezone.localdate() + timedelta(days=1)
        if field == "available_on"
        else timezone.localdate()
    )
    changes = {field: value}
    if field == "charge_before":
        changes["available_on"] = timezone.localdate() - timedelta(days=2)
    ChannelCardCollection.objects.filter(pk=order.pk).update(**changes)
    with pytest.raises(ValidationError):
        service.start_checkout(order.pk, actor=rig.owner, client=rig.gateway, hostaway=rig.remote)
    rig.gateway.create_checkout.assert_not_called()


def test_changed_booking_blocks_start_without_changing_order_amount(rig):
    order = draft(rig)
    rig.remote.get_reservation.return_value.total_price = Decimal("950")
    with pytest.raises(ValidationError):
        service.start_checkout(order.pk, actor=rig.owner, client=rig.gateway, hostaway=rig.remote)
    rig.gateway.create_checkout.assert_not_called()
    order.refresh_from_db()
    assert order.amount == Decimal("670.24")


def test_uncertain_checkout_claim_survives_and_can_never_be_reposted(rig):
    order = draft(rig)
    rig.gateway.create_checkout.side_effect = HyperPayConnectionError()
    with pytest.raises(HyperPayError):
        service.start_checkout(order.pk, actor=rig.owner, client=rig.gateway, hostaway=rig.remote)
    order.refresh_from_db()
    assert order.status == "unknown"
    with pytest.raises(ValidationError):
        service.start_checkout(order.pk, actor=rig.owner, client=rig.gateway, hostaway=rig.remote)
    with pytest.raises(ValidationError):
        service.reconcile(order.pk, actor=rig.owner, client=rig.gateway)
    assert rig.gateway.create_checkout.call_count == 1


@pytest.mark.parametrize(
    "changes",
    [
        {"amount": "670.23"},
        {"currency": "EUR"},
        {"merchantTransactionId": "wrong"},
        {"entityId": "another-merchant"},
        {"paymentType": "PA"},
        {"paymentBrand": "MADA"},
        {"id": ""},
        {"testMode": "EXTERNAL"},
    ],
)
def test_server_rejects_wrong_payment_identity_amount_currency_account_brand_or_mode(rig, changes):
    order = started(rig)
    rig.gateway.get_checkout_payment.return_value = document(order, **changes)
    assert service.reconcile(order.pk, actor=rig.owner, client=rig.gateway).status == "review"
    assert not MarketingEventReceipt.objects.exists()


@pytest.mark.parametrize(
    "code,status",
    [
        ("000.200.100", "pending"),
        ("100.396.101", "cancelled"),
        ("800.100.100", "failed"),
        ("100.390.111", "failed"),
        ("000.400.100", "review"),
        ("bad", "unknown"),
    ],
)
def test_authentication_decline_pending_and_review_never_open_another_checkout(rig, code, status):
    order = started(rig)
    rig.gateway.get_checkout_payment.return_value = document(order, result={"code": code})
    assert service.reconcile(order.pk, actor=rig.owner, client=rig.gateway).status == status
    with pytest.raises(ValidationError):
        service.start_checkout(order.pk, actor=rig.owner, client=rig.gateway, hostaway=rig.remote)
    assert rig.gateway.create_checkout.call_count == 1


def test_capture_identity_cannot_move_between_channel_orders(rig):
    first = started(rig)
    rig.gateway.get_checkout_payment.return_value = document(first)
    first = service.reconcile(first.pk, actor=rig.owner, client=rig.gateway)
    second = ChannelCardCollection.objects.create(
        hostaway_reservation_id="930002",
        property=rig.place,
        amount=first.amount,
        currency="SAR",
        available_on=first.available_on,
        charge_before=first.charge_before,
        balance_checked_at=timezone.now(),
        payer=first.payer,
        environment=first.environment,
        entity_fingerprint=first.entity_fingerprint,
        merchant_transaction_id="synthetic-another-transaction",
        provider_checkout_id="synthetic-checkout-5678",
        status="pending",
        created_by=rig.owner,
    )
    rig.gateway.get_checkout_payment.return_value = document(second)
    result = service.reconcile(second.pk, actor=rig.owner, client=rig.gateway)
    assert result.status == "review" and result.failure_code == "payment_id_already_recorded"
    with transaction.atomic():
        assert payment_id_conflict(first.provider_payment_id, PaymentAttempt())


def test_channel_paid_label_is_not_merchant_receipt_but_actual_local_money_blocks(rig):
    from apps.payments.models import OfflinePaymentReceipt

    local = Reservation.objects.create(
        hostaway_reservation_id=930001,
        property=rig.place,
        source_type="external_channel",
        payment_status="paid",
        normalized_status="confirmed",
        check_in=timezone.localdate() - timedelta(days=2),
        check_out=timezone.localdate() - timedelta(days=1),
        nights=1,
        guests=1,
        currency="SAR",
        total_price=Decimal("900"),
    )
    order = draft(rig)
    assert order.reservation_id == local.pk
    OfflinePaymentReceipt.objects.create(
        reservation=local,
        amount=Decimal("670.24"),
        currency="SAR",
        method="bank_transfer",
        reference="synthetic-local-receipt",
        received_at=timezone.now(),
        recorded_by=rig.owner,
        snapshot_digest="synthetic-only",
    )
    with pytest.raises(ValidationError):
        service.start_checkout(order.pk, actor=rig.owner, client=rig.gateway, hostaway=rig.remote)
    rig.gateway.create_checkout.assert_not_called()


def test_private_views_never_trust_url_or_allow_guest_collection(rig, monkeypatch, settings):
    order = draft(rig)
    detail = reverse("notifications:channel_collection_detail", args=[order.pk])
    anonymous = Client()
    assert anonymous.get(detail).status_code == 302
    guest = get_user_model().objects.create_user(username="guest", email="guest@example.invalid")
    anonymous.force_login(guest)
    assert anonymous.get(detail).status_code == 403
    assert anonymous.post(detail, {"action": "start"}).status_code == 403
    client = Client()
    client.force_login(rig.owner)
    settings.BOOKING_VCC_COLLECTION_ENABLED = False
    page = client.get(detail)
    assert page.status_code == 200 and b"no-store" in page["Cache-Control"].encode()
    assert (
        b"googletagmanager.com" not in page.content and b"data-purchase-event" not in page.content
    )
    assert client.post(detail, {"action": "start"}).status_code == 302
    order.refresh_from_db()
    assert order.status == "draft"
    mocked = Mock()
    monkeypatch.setattr("apps.payments.channel_views.reconcile", mocked)
    result_url = reverse("notifications:channel_collection_result", args=[order.pk])
    assert client.get(result_url, {"id": "browser-success"}).status_code == 403
    mocked.assert_not_called()
    response = client.get(
        result_url,
        {
            "return_token": _hyperpay_return_token(order),
            "resourcePath": "https://invalid.example/forged",
        },
    )
    assert response.status_code == 302 and response.url == detail
    mocked.assert_called_once_with(order.pk, actor=rig.owner)
    assert client.post(detail, {"action": "refresh"}).status_code == 302


@pytest.mark.django_db(transaction=True)
def test_postgres_concurrent_clicks_create_only_one_gateway_checkout(rig):
    if connection.vendor != "postgresql":
        pytest.skip("Production row locks require PostgreSQL")
    order = draft(rig)
    barrier = Barrier(2)

    def run():
        close_old_connections()
        try:
            actor = get_user_model().objects.get(pk=rig.owner.pk)
            barrier.wait(timeout=10)
            try:
                service.start_checkout(
                    order.pk, actor=actor, client=rig.gateway, hostaway=rig.remote
                )
                return "started"
            except ValidationError:
                return "blocked"
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: run(), range(2)))
    assert sorted(results) == ["blocked", "started"]
    assert rig.gateway.create_checkout.call_count == 1


def test_render_channel_collection_layouts(rig, settings, monkeypatch, tmp_path):
    import os
    from pathlib import Path

    from tests.test_contact_layout import _standalone

    settings.BOOKING_VCC_COLLECTION_ENABLED = False
    monkeypatch.setattr(
        "apps.payments.channel_views.address_context",
        lambda request: {
            "current_language_code": "ar",
            "address_places_enabled": True,
            "address_places_ticket": "synthetic-ticket",
            "address_suggestions_url": "/synthetic-suggestions",
            "address_details_url": "/synthetic-details",
        },
    )
    client = Client()
    client.force_login(rig.owner)
    output = Path(os.environ.get("LSA_CONTACT_VISUAL_DIR", tmp_path))
    output.mkdir(parents=True, exist_ok=True)
    order = draft(rig)
    pages = {"channel-list": client.get(reverse("notifications:channel_collection_list"))}
    assert pages["channel-list"].status_code == 200
    assert "data-address-search" in pages["channel-list"].content.decode()
    detail = reverse("notifications:channel_collection_detail", args=[order.pk])
    for state in ("draft", "pending", "succeeded"):
        order.status = state
        order.save(update_fields=["status"])
        pages["channel-" + state] = client.get(detail)
    for name, response in pages.items():
        assert response.status_code == 200
        html = response.content.decode()
        assert "googletagmanager.com" not in html and "paymentWidgets" not in html
        assert 'value="start"' not in html
        (output / (name + ".html")).write_text(_standalone(html), encoding="utf-8")


def test_manager_gateway_page_uses_only_hosted_visa_master_and_private_return(rig, monkeypatch):
    order = started(rig)
    monkeypatch.setattr("apps.payments.channel_views.start_checkout", Mock(return_value=order))
    client = Client()
    client.force_login(rig.owner)
    page = client.post(
        reverse("notifications:channel_collection_detail", args=[order.pk]), {"action": "start"}
    )
    assert page.status_code == 200
    html = page.content.decode()
    assert 'data-brands="VISA MASTER"' in html
    assert 'integrity="sha384-YWJj"' in html and 'crossorigin="anonymous"' in html
    assert "return_token=" in html
    assert "billing@example.invalid" not in html
    assert 'name="card_number"' not in html and 'name="cvc"' not in html
    assert "googletagmanager.com" not in html and "payment-journey.js" not in html
    assert "oppwa.com" in page["Content-Security-Policy"]


def test_revoked_manager_loses_all_collection_routes(rig):
    from apps.accounts.access import MANAGER_SESSION_KEY
    from apps.accounts.models import OperationsManager

    order = draft(rig)
    user = get_user_model().objects.create_user(username="manager", email="manager@gmail.com")
    grant = OperationsManager.objects.create(email=user.email, created_by=rig.owner)
    client = Client()
    client.force_login(user)
    session = client.session
    session[MANAGER_SESSION_KEY] = {
        "user_id": user.pk,
        "email": user.email,
        "grant_id": grant.pk,
        "version": grant.access_version,
    }
    session.save()
    detail = reverse("notifications:channel_collection_detail", args=[order.pk])
    assert client.get(detail).status_code == 200
    grant.is_enabled = False
    grant.save(update_fields=["is_enabled"])
    for url in (
        detail,
        reverse("notifications:channel_collection_list"),
        reverse("notifications:channel_collection_result", args=[order.pk]),
    ):
        assert client.get(url).status_code == 403
        assert client.post(url, {"action": "start"}).status_code == 403


def local_external(rig):
    return Reservation.objects.create(
        hostaway_reservation_id=930001,
        property=rig.place,
        source_type="external_channel",
        payment_status="unpaid",
        normalized_status="confirmed",
        check_in=timezone.localdate() - timedelta(days=2),
        check_out=timezone.localdate() - timedelta(days=1),
        nights=1,
        guests=1,
        currency="SAR",
        total_price=Decimal("670.24"),
    )


def offline_values(local, actor):
    from apps.payments.offline_receipts import receipt_snapshot

    return {
        "request": SimpleNamespace(user=actor),
        "reservation_id": local.pk,
        "amount": local.total_price,
        "currency": local.currency,
        "method": "bank_transfer",
        "reference": "synthetic-vcc-manual-receipt",
        "received_at": timezone.now() - timedelta(minutes=1),
        "snapshot_digest": receipt_snapshot(local),
        "confirmed": True,
    }


@pytest.mark.parametrize(
    "state", ["created", "pending", "unknown", "review", "succeeded", "failed", "cancelled"]
)
def test_started_channel_order_blocks_manual_receipt_even_without_local_foreign_key(rig, state):
    from apps.payments.models import OfflinePaymentReceipt
    from apps.payments.offline_receipts import record_offline_receipt

    order = draft(rig)
    local = local_external(rig)
    order.status = state
    order.save(update_fields=["status"])
    assert order.reservation_id is None
    with pytest.raises(ValidationError, match="تحصيل قناة"):
        record_offline_receipt(**offline_values(local, rig.owner))
    assert not OfflinePaymentReceipt.objects.exists()


def test_original_manual_receipt_is_preserved_and_unstarted_channel_order_closed(rig, monkeypatch):
    from apps.payments.offline_receipts import record_offline_receipt

    monkeypatch.setattr("apps.loyalty.site_adapter.schedule_reconciliation", Mock())
    monkeypatch.setattr("apps.reservations.stay_extensions.capture_reservation_change", Mock())
    local = local_external(rig)
    order = draft(rig)
    values = offline_values(local, rig.owner)
    receipt = record_offline_receipt(**values)
    assert record_offline_receipt(**values).pk == receipt.pk
    order.refresh_from_db()
    assert order.status == "review" and order.failure_code == "offline_receipt_recorded"
    with pytest.raises(ValidationError):
        service.start_checkout(order.pk, actor=rig.owner, client=rig.gateway, hostaway=rig.remote)
    rig.gateway.create_checkout.assert_not_called()


@pytest.mark.django_db(transaction=True)
def test_postgres_manual_receipt_racing_channel_claim_has_only_one_winner(rig, monkeypatch):
    from apps.payments.models import OfflinePaymentReceipt
    from apps.payments.offline_receipts import record_offline_receipt

    if connection.vendor != "postgresql":
        pytest.skip("Cross-workflow lock requires PostgreSQL")
    monkeypatch.setattr("apps.loyalty.site_adapter.schedule_reconciliation", Mock())
    monkeypatch.setattr("apps.reservations.stay_extensions.capture_reservation_change", Mock())
    local = local_external(rig)
    order = draft(rig)
    barrier = Barrier(2)

    def run(kind):
        close_old_connections()
        try:
            actor = get_user_model().objects.get(pk=rig.owner.pk)
            values = offline_values(local, actor)
            barrier.wait(timeout=10)
            try:
                if kind == "channel":
                    service.start_checkout(
                        order.pk, actor=actor, client=rig.gateway, hostaway=rig.remote
                    )
                else:
                    record_offline_receipt(**values)
                return kind
            except ValidationError:
                return "blocked"
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(run, ["channel", "manual"]))
    assert results.count("blocked") == 1
    assert rig.gateway.create_checkout.call_count + OfflinePaymentReceipt.objects.count() == 1


def test_manager_posts_require_csrf(rig):
    order = draft(rig)
    client = Client(enforce_csrf_checks=True)
    client.force_login(rig.owner)
    assert (
        client.post(
            reverse("notifications:channel_collection_detail", args=[order.pk]), {"action": "start"}
        ).status_code
        == 403
    )


def test_verified_capture_kept_for_review_when_legacy_receipt_appears_after_claim(rig):
    from apps.payments.models import OfflinePaymentReceipt
    from apps.payments.offline_receipts import receipt_snapshot

    local = local_external(rig)
    order = started(rig)
    # Simulate an already-present external/legacy receipt, not a permitted new
    # receipt call: the current receipt service now blocks this reverse race.
    receipt = OfflinePaymentReceipt.objects.create(
        reservation=local,
        amount=local.total_price,
        currency=local.currency,
        method="bank_transfer",
        reference="synthetic-legacy-late-receipt",
        received_at=timezone.now() - timedelta(minutes=1),
        recorded_by=rig.owner,
        snapshot_digest=receipt_snapshot(local),
    )
    original = (
        receipt.pk,
        receipt.amount,
        receipt.reference,
        receipt.received_at,
        receipt.snapshot_digest,
    )
    before = counts()
    rig.gateway.get_checkout_payment.return_value = document(order)
    for _ in range(2):
        result = service.reconcile(order.pk, actor=rig.owner, client=rig.gateway)
        assert result.status == "review" and result.failure_code == "local_collection_changed"
        assert result.provider_payment_id == "synthetic-capture-1234" and result.verified_at
        assert result.provider_result_code == "000.000.000"
    receipt.refresh_from_db()
    assert (
        receipt.pk,
        receipt.amount,
        receipt.reference,
        receipt.received_at,
        receipt.snapshot_digest,
    ) == original
    assert OfflinePaymentReceipt.objects.count() == 1 and counts() == before
    with pytest.raises(ValidationError):
        service.start_checkout(order.pk, actor=rig.owner, client=rig.gateway, hostaway=rig.remote)
    assert rig.gateway.create_checkout.call_count == 1
    assert rig.gateway.get_checkout_payment.call_count == 2
