"""Amended balances: real DB/owner forms, fake payment and Hostaway providers."""

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from decimal import Decimal
from threading import Barrier
from unittest.mock import patch
from uuid import uuid4

import pytest
from django.db import close_old_connections, connection
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from apps.payments.hyperpay.exceptions import HyperPayCheckoutError
from apps.payments.hyperpay.service import HyperPayService
from apps.payments.models import PaymentAttempt, ReservationPaymentRequest
from apps.payments.payment_link_revisions import (
    approve_preview,
    prepare_preview,
    projected_reservation,
)
from apps.payments.payment_link_tasks import payment_message
from apps.payments.payment_links import (
    complete_request_payment,
    issue_request,
    link_token,
    public_url,
    resolve_token,
    validate_open,
)
from apps.reservations.models import Reservation
from apps.reservations.services.hostaway_modifications import HostawayModificationService
from apps.reservations.services.modifications import ModificationService
from tests.test_booking_modifications_phase6 import (
    ModificationAvailabilityStub,
    WriteClientStub,
    create_extension,
    price_quote,
    updated_snapshot,
)
from tests.test_manual_booking_operations_phase61 import owner
from tests.test_reservation_payment_links import (
    approved_document,
    checkout,
    form_data,
    isolated_settings,  # noqa: F401
    manual_stay,
    page_url,
    remote,
)

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def amendment_settings(settings):
    settings.HOSTAWAY_LIVE_MODIFICATION_ENABLED = True
    settings.HOSTAWAY_LIVE_EXTENSION_ENABLED = True


def collect(stay, amount, *, request=None, status="succeeded"):
    return PaymentAttempt.objects.create(
        booking_intent=stay.booking_intent,
        payment_request=request,
        provider="hyperpay",
        amount=Decimal(amount),
        currency="SAR",
        status=status,
        verified_at=timezone.now() if status == "succeeded" else None,
        provider_payment_id=uuid4().hex if status == "succeeded" else None,
        merchant_transaction_id=uuid4().hex,
        idempotency_key=uuid4().hex,
    )


def change(stay, total="2142"):
    quote = price_quote(stay, total=Decimal(total))
    modification = create_extension(
        stay, stub=ModificationAvailabilityStub(stay, quote=quote)
    ).request
    ModificationService().set_owner_final_total(modification, final_total=Decimal(total))
    modification.refresh_from_db()
    return modification


def activate(draft):
    modification = draft.modification_request
    stay = modification.reservation
    quote = price_quote(
        stay,
        check_in=modification.new_check_in,
        check_out=modification.new_check_out,
        total=Decimal(modification.quote_snapshot["system_total"]),
    )
    writer = WriteClientStub(snapshot=updated_snapshot(modification))
    service = HostawayModificationService(
        client=writer,
        modification_service=ModificationService(
            availability_service=ModificationAvailabilityStub(stay, quote=quote)
        ),
    )
    result = approve_preview(
        draft.pk, modification_service=service, hostaway=remote(projected_reservation(modification))
    )
    return result, writer


def owner_client():
    client = Client()
    client.force_login(owner())
    return client


def test_preview_is_inactive_and_old_url_switches_only_after_approval():
    stay = manual_stay()
    old = issue_request(stay.pk)
    old_url = public_url(old)
    modification = change(stay)
    draft = prepare_preview(stay.pk, modification.pk)
    assert draft.status == "draft" and draft.approved_at is None
    assert draft.amount_sar == Decimal("2142")
    assert public_url(draft) == old_url
    old.refresh_from_db()
    assert old.status == "open" and old.superseded_by_id is None
    assert resolve_token(link_token(old)).pk == old.pk
    with pytest.raises(HyperPayCheckoutError):
        validate_open(draft)
    assert not PaymentAttempt.objects.exists()
    assert not draft.deliveries.exists()
    assert not modification.hostaway_operations.exists()
    active, writer = activate(draft)
    old.refresh_from_db()
    assert active.status == "open" and active.approved_at is not None
    assert old.status == "revoked" and old.amount_sar == Decimal("1250")
    assert resolve_token(link_token(old)).pk == active.pk
    assert public_url(active) == old_url
    assert issue_request(stay.pk).pk == active.pk
    assert writer.calls == 1
    assert Reservation.objects.count() == 1
    assert not PaymentAttempt.objects.exists()
    assert not active.deliveries.exists()
    assert b'name="billing_city"' in Client().get(page_url(old)).content
    assert b"googletagmanager.com" not in Client().get(page_url(old)).content


@pytest.mark.parametrize("paid", ["0", "500", "1250"])
def test_full_or_remaining_amount_is_frozen_and_paid_once(paid):
    stay = manual_stay()
    old = issue_request(stay.pk)
    if Decimal(paid):
        collect(stay, paid, request=old)
        old.status = "paid"
        old.save(update_fields=["status"])
    modification = change(stay)
    draft = prepare_preview(stay.pk, modification.pk)
    assert draft.source_amount == Decimal("2142") - Decimal(paid)
    active, _ = activate(draft)
    assert active.amount_sar == Decimal("2142") - Decimal(paid)
    session = checkout(active)
    assert session.attempt.amount == active.amount_sar
    gateway = session.attempt
    gateway.status = "succeeded"
    gateway.verified_at = timezone.now()
    gateway.provider_payment_id = uuid4().hex
    gateway.save()
    complete_request_payment(gateway)
    complete_request_payment(gateway)
    active.refresh_from_db()
    stay.refresh_from_db()
    assert active.status == "paid" and stay.payment_status == "paid"
    assert stay.total_price == Decimal("2142")
    assert Reservation.objects.count() == 1
    assert PaymentAttempt.objects.filter(payment_request=active).count() == 1
    with pytest.raises(HyperPayCheckoutError):
        checkout(active)


def test_verified_callback_uses_remaining_amount_not_original_intent_total():
    from tests.test_hyperpay import HyperPayStub

    stay = manual_stay()
    collect(stay, "500")
    modification = change(stay)
    active, _ = activate(prepare_preview(stay.pk, modification.pk))
    stub = HyperPayStub()
    session = checkout(active, stub=stub)
    assert session.attempt.amount == Decimal("1642")
    stub.payment_document = approved_document(session.attempt)
    HyperPayService(client=stub).verify(session.attempt)
    active.refresh_from_db()
    assert active.status == "paid"
    assert stay.booking_intent.total_price == Decimal("1250")


@pytest.mark.parametrize("change_kind", ["paid", "pending", "price", "expired", "contact"])
def test_changed_preview_never_writes_hostaway_or_activates(change_kind):
    stay = manual_stay()
    old = issue_request(stay.pk)
    modification = change(stay)
    draft = prepare_preview(stay.pk, modification.pk)
    if change_kind == "paid":
        collect(stay, "1")
    elif change_kind == "pending":
        collect(stay, "1", status="pending")
    elif change_kind == "price":
        ModificationService().set_owner_final_total(modification, final_total=Decimal("2200"))
    elif change_kind == "expired":
        draft.expires_at = timezone.now() - timedelta(seconds=1)
        draft.save(update_fields=["expires_at"])
    else:
        stay.booking_intent.guest_phone = "+16502530001"
        stay.booking_intent.save(update_fields=["guest_phone"])
    with pytest.raises(HyperPayCheckoutError):
        activate(draft)
    draft.refresh_from_db()
    old.refresh_from_db()
    assert draft.status == "draft" and old.superseded_by_id is None
    assert not modification.hostaway_operations.exists()


def test_prior_unresolved_checkout_prevents_preview():
    stay = manual_stay()
    original = issue_request(stay.pk)
    checkout(original)
    modification = change(stay)
    with pytest.raises(HyperPayCheckoutError, match="previous_payment_unresolved"):
        prepare_preview(stay.pk, modification.pk)


def test_settled_booking_needs_no_payment_link():
    stay = manual_stay()
    collect(stay, "2142")
    modification = change(stay)
    with pytest.raises(HyperPayCheckoutError, match="nothing_due"):
        prepare_preview(stay.pk, modification.pk)


def test_late_old_payment_is_reviewed_and_blocks_updated_link():
    stay = manual_stay()
    old = issue_request(stay.pk)
    original_attempt = collect(stay, "1250", request=old, status="failed")
    modification = change(stay)
    active, _ = activate(prepare_preview(stay.pk, modification.pk))
    original_attempt.status = "succeeded"
    original_attempt.verified_at = timezone.now()
    original_attempt.save()
    complete_request_payment(original_attempt)
    old.refresh_from_db()
    assert old.status == "review"
    with pytest.raises(HyperPayCheckoutError):
        validate_open(active)
    active.refresh_from_db()
    assert active.status == "open"


def test_owner_preview_get_and_missing_confirmation_never_send():
    client = owner_client()
    stay = manual_stay()
    old = issue_request(stay.pk)
    modification = change(stay)
    url = reverse("notifications:updated_payment_link_preview", args=[stay.pk])
    assert client.get(url).status_code == 405
    response = client.post(url, {"modification_id": modification.pk})
    assert response.status_code == 302
    review = client.get(response.url)
    assert review.status_code == 200 and "no-store" in review["Cache-Control"]
    assert "الرسالة التي ستُرسل" in review.content.decode()
    assert "المتبقي المطلوب تحصيله" in review.content.decode()
    assert public_url(old) in review.content.decode()
    assert client.post(response.url, {}).status_code == 200
    draft = ReservationPaymentRequest.objects.get(status="draft")
    assert not draft.deliveries.exists()
    assert not modification.hostaway_operations.exists()
    assert not PaymentAttempt.objects.exists()
    booking = client.get(reverse("notifications:booking_detail", args=[stay.pk]))
    assert "تحديث رابط الدفع ومراجعته" in booking.content.decode()
    assert "إرسال طلب تكوين رابط الدفع إلى أسيل" in booking.content.decode()


def test_owner_approval_updates_and_queues_once_without_fallback_message():
    client = owner_client()
    stay = manual_stay()
    modification = change(stay)
    draft = prepare_preview(stay.pk, modification.pk)
    url = reverse("notifications:updated_payment_link_review", args=[stay.pk, draft.pk])
    with patch(
        "apps.payments.owner_payment_links.approve_preview",
        side_effect=lambda pk: activate(draft)[0],
    ):
        first = client.post(url, {"confirm_updated_link": "on"})
        second = client.post(url, {"confirm_updated_link": "on"})
    assert first.status_code == second.status_code == 302
    draft.refresh_from_db()
    assert draft.status == "open"
    assert draft.deliveries.count() == 1
    assert draft.deliveries.get().status == "queued"
    assert draft.modification_request.hostaway_operations.count() == 1


@pytest.mark.parametrize("language", ["ar", "en", "fr"])
def test_revised_message_and_guest_page_show_new_dates_and_balance(language):
    stay = manual_stay(language=language)
    collect(stay, "500")
    modification = change(stay)
    active, _ = activate(prepare_preview(stay.pk, modification.pk))
    body = payment_message(active)
    assert public_url(active) in body
    response = Client().get(page_url(active))
    assert response.status_code == 200
    assert response.context["payment_request"].source_amount == Decimal("1642")
    assert response.context["payment_request"].reservation.check_out == modification.new_check_out
    assert "no-store" in response["Cache-Control"]


def test_foreign_unpaid_stay_uses_frozen_due_fx_for_capture():
    from apps.payments.currency import CurrencyService
    from tests.test_currency import fixed_rates

    stay = manual_stay(currency="MAD")
    quote = price_quote(stay, total=Decimal("2140"), currency="MAD")
    with patch.object(CurrencyService, "get_rates", return_value=fixed_rates()):
        modification = create_extension(
            stay, stub=ModificationAvailabilityStub(stay, quote=quote)
        ).request
        ModificationService().set_owner_final_total(modification, final_total=Decimal("2140"))
        draft = prepare_preview(stay.pk, modification.pk)
    modification.refresh_from_db()
    # External modification already confirmed; approval is still required.
    stay.check_out = modification.new_check_out
    stay.total_price = modification.new_total
    stay.normalized_status = "modified"
    stay.save()
    modification.status = "completed"
    modification.save()
    active = approve_preview(draft.pk, hostaway=remote(stay))
    session = checkout(active)
    attempt = session.attempt
    attempt.status, attempt.verified_at = "succeeded", timezone.now()
    attempt.save()
    complete_request_payment(attempt)
    active.refresh_from_db()
    assert active.status == "paid"
    assert active.source_currency == "MAD" and active.source_amount == Decimal("2140")
    assert attempt.currency == "SAR" and attempt.amount == active.amount_sar


def test_remote_read_failure_preserves_confirmed_change_without_sending():
    from apps.integrations.hostaway.exceptions import HostawayTimeoutError

    stay = manual_stay()
    old = issue_request(stay.pk)
    modification = change(stay)
    draft = prepare_preview(stay.pk, modification.pk)
    with (
        patch(
            "apps.payments.payment_link_revisions.verify_revision_remote",
            side_effect=HostawayTimeoutError(),
        ),
        pytest.raises(HostawayTimeoutError),
    ):
        activate(draft)
    modification.refresh_from_db()
    old.refresh_from_db()
    draft.refresh_from_db()
    assert modification.status == "completed"
    assert draft.status == "draft" and old.superseded_by_id is None
    assert not draft.deliveries.exists()
    stay.refresh_from_db()
    active = approve_preview(draft.pk, hostaway=remote(stay))
    assert active.status == "open"
    assert modification.hostaway_operations.count() == 1


def test_remote_unmatched_collection_stops_activation_and_checkout():
    stay = manual_stay()
    modification = change(stay)
    draft = prepare_preview(stay.pk, modification.pk)
    active, _ = activate(draft)
    hostaway = remote(active.reservation)
    hostaway.get_guest_charges.return_value = [
        {"status": "paid", "amount": "100", "currency": "SAR", "title": "Other collection"}
    ]
    with pytest.raises(HyperPayCheckoutError, match="unmatched_hostaway_collection"):
        checkout(active, hostaway=hostaway)
    assert not PaymentAttempt.objects.exists()


def test_known_prior_hostaway_receipt_allows_only_remaining_balance():
    stay = manual_stay()
    prior = collect(stay, "500")
    modification = change(stay)
    active, _ = activate(prepare_preview(stay.pk, modification.pk))
    hostaway = remote(active.reservation)
    hostaway.get_reservation.return_value.payment_status = "partially_paid"
    hostaway.get_guest_charges.return_value = [
        {
            "status": "paid",
            "amount": "500",
            "currency": "SAR",
            "title": f"LSA HyperPay receipt {prior.pk}",
        }
    ]
    assert checkout(active, hostaway=hostaway).attempt.amount == Decimal("1642")


def test_pending_checkout_resumes_instead_of_creating_a_second_charge():
    stay = manual_stay()
    modification = change(stay)
    active, _ = activate(prepare_preview(stay.pk, modification.pk))
    first = checkout(active)
    second = checkout(active)
    assert first.attempt.pk == second.attempt.pk
    assert active.attempts.count() == 1


def test_partial_balance_is_not_shown_as_fully_paid_in_admin(settings):
    client = owner_client()
    stay = manual_stay()
    settings.HYPERPAY_ENVIRONMENT = "production"
    stay.is_test = False
    stay.save(update_fields=["is_test"])
    collect(stay, "500")
    modification = change(stay)
    active, _ = activate(prepare_preview(stay.pk, modification.pk))
    page = client.get(reverse("notifications:booking_detail", args=[stay.pk]))
    assert not page.context["detail_payment_settled"]
    assert page.context["guest_payment_request"].pk == active.pk
    assert (
        "إعادة إرسال رابط السداد" in page.content.decode()
        or "رابط السداد للضيف" in page.content.decode()
    )
    listing = client.get(reverse("notifications:booking_list"))
    item = next(row for row in listing.context["bookings"] if row.pk == stay.pk)
    assert item.admin_status_code == "awaiting_payment"


def test_new_cancellation_blocks_existing_updated_link():
    stay = manual_stay()
    modification = change(stay)
    active, _ = activate(prepare_preview(stay.pk, modification.pk))
    ModificationService.create_cancellation_request(
        stay, session_hash="owner:test", owner_override=True
    )
    with pytest.raises(HyperPayCheckoutError):
        validate_open(active)


@pytest.mark.django_db(transaction=True)
def test_database_default_accepts_pre_release_payment_request_writer():
    if connection.vendor != "postgresql":
        pytest.skip("Rolling deploy compatibility on PostgreSQL")
    stay = manual_stay()
    identifier = uuid4()
    with connection.cursor() as cursor:
        cursor.execute(
            "INSERT INTO payments_reservationpaymentrequest "
            "(id, reservation_id, amount_sar, source_amount, source_currency, fingerprint, "
            "language, environment, status, legal_acceptance, review_code, created_at) "
            "VALUES (%s,%s,10,10,'SAR','old-writer','ar','test','revoked','{}','',%s)",
            [identifier, stay.pk, timezone.now()],
        )
    assert ReservationPaymentRequest.objects.get(pk=identifier).settlement_snapshot == {}


@pytest.mark.django_db(transaction=True)
def test_postgres_concurrent_approval_keeps_one_active_request():
    if connection.vendor != "postgresql":
        pytest.skip("Requires PostgreSQL row locks")
    stay = manual_stay()
    old = issue_request(stay.pk)
    modification = change(stay)
    draft = prepare_preview(stay.pk, modification.pk)
    active, _ = activate(draft)
    barrier = Barrier(2)

    def approve_again():
        close_old_connections()
        try:
            barrier.wait(timeout=10)
            return approve_preview(active.pk).pk
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: approve_again(), range(2)))
    assert results == [active.pk, active.pk]
    assert stay.payment_requests.filter(status="open").count() == 1
    assert resolve_token(link_token(old)).pk == active.pk


def test_review_and_approval_are_private_and_scoped_to_the_booking():
    from django.contrib.auth import get_user_model

    stay = manual_stay()
    modification = change(stay)
    draft = prepare_preview(stay.pk, modification.pk)
    url = reverse("notifications:updated_payment_link_review", args=[stay.pk, draft.pk])
    assert Client().get(url).status_code == 302
    denied = Client()
    denied.force_login(get_user_model().objects.create_user("guest", "guest@example.invalid"))
    assert (
        denied.get(url).status_code
        == denied.post(url, {"confirm_updated_link": "on"}).status_code
        == 403
    )
    client = owner_client()
    wrong = reverse("notifications:updated_payment_link_review", args=[uuid4(), draft.pk])
    assert client.get(wrong).status_code == 404
    assert not draft.deliveries.exists()
    assert not modification.hostaway_operations.exists()


def test_old_open_guest_form_must_review_new_amount_before_payment():
    from tests.test_hyperpay import HyperPayStub

    stay = manual_stay()
    old = issue_request(stay.pk)
    modification = change(stay)
    active, _ = activate(prepare_preview(stay.pk, modification.pk))
    client = Client()
    data = form_data() | {"payment_request_version": str(old.pk)}
    response = client.post(page_url(old), data)
    assert response.status_code == 200
    assert "paymentWidgets.js" not in response.content.decode()
    assert not PaymentAttempt.objects.exists()
    assert response.context["payment_request"].pk == active.pk
    data["payment_request_version"] = str(active.pk)
    with (
        patch("apps.payments.payment_links.HyperPayService", wraps=HyperPayService),
        patch("apps.payments.hyperpay.service.HyperPayClient", return_value=HyperPayStub()),
        patch(
            "apps.integrations.hostaway.client.HostawayClient",
        ) as hostaway_class,
    ):
        hostaway_class.return_value.__enter__.return_value = remote(active.reservation)
        response = client.post(page_url(old), data)
    assert response.status_code == 200
    assert response.context["attempt"].amount == Decimal("2142")
    assert response.context["payment_request"].pk == active.pk


@pytest.mark.django_db(transaction=True)
def test_postgres_two_first_approvals_share_one_new_active_link():
    if connection.vendor != "postgresql":
        pytest.skip("Requires PostgreSQL row locks")
    stay = manual_stay()
    old = issue_request(stay.pk)
    modification = change(stay)
    # Aseel/the normal admin path has already confirmed the same stay.
    stay.check_out, stay.total_price = modification.new_check_out, modification.new_total
    stay.normalized_status = "modified"
    stay.save()
    modification.status = "completed"
    modification.save()
    draft = prepare_preview(stay.pk, modification.pk)
    barrier = Barrier(2)

    def approve_first():
        close_old_connections()
        try:
            read_client = remote(stay)
            snapshot = read_client.get_reservation.return_value

            def concurrent_read(*args):
                barrier.wait(timeout=10)
                return snapshot

            read_client.get_reservation.side_effect = concurrent_read
            return approve_preview(draft.pk, hostaway=read_client).pk
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: approve_first(), range(2)))
    assert results == [draft.pk, draft.pk]
    assert stay.payment_requests.filter(status="open").count() == 1
    assert resolve_token(link_token(old)).pk == draft.pk
