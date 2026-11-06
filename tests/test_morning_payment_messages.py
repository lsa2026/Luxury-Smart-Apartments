"""Synthetic morning payment notices; all remote effects are mocked."""

import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier
from unittest.mock import Mock, patch

import pytest
from django.db import close_old_connections, connection, transaction
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from apps.notifications.services.ultramsg import UltraMsgConnectionError
from apps.payments.models import MorningPaymentAttempt
from apps.reservations import morning_payment_messages as links
from apps.reservations.models import MorningPaymentLinkDelivery, MorningStay
from apps.reservations.morning_stays import MorningStayError, create_stay
from tests.test_hyperpay import HYPERPAY_SETTINGS
from tests.test_morning_stays import create

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def isolated(settings):
    for key, value in HYPERPAY_SETTINGS.items():
        setattr(settings, key, value)
    settings.SITE_BASE_URL = "https://testserver"
    settings.ULTRAMSG_ENABLED = True
    settings.ULTRAMSG_INSTANCE_ID = "synthetic"
    settings.ULTRAMSG_TOKEN = "synthetic"
    with patch("httpx.Client.request", side_effect=AssertionError("unexpected live call")):
        yield


def stay_without_notice(settings):
    settings.ULTRAMSG_ENABLED = False
    stay = create()
    settings.ULTRAMSG_ENABLED = True
    return stay


def test_new_stay_needs_explicit_send_intent_only_after_commit(
    django_capture_on_commit_callbacks,
):
    with patch.object(links, "dispatch") as dispatch:
        with django_capture_on_commit_callbacks(execute=True):
            stay = create()
            assert not MorningPaymentLinkDelivery.objects.filter(stay=stay).exists()
            links.queue_initial(stay)
            dispatch.assert_not_called()
        dispatch.assert_called_once()
        links.queue_initial(stay)
        assert MorningPaymentLinkDelivery.objects.count() == 1
    text = links.render_message(stay)
    assert text.count(links.public_url(stay)) == 1
    assert "تفاصيل الحجز الصباحي" in text and "بعد نجاح الدفع" in text
    assert "500.00" in text and "Asia/Riyadh" in text
    assert "تعليمات الدخول" in text and "gate_code" not in text
    assert not stay.communication_events.exists()


def test_rollback_never_dispatches_or_leaves_a_notice():
    with patch.object(links, "dispatch") as dispatch:
        with pytest.raises(RuntimeError), transaction.atomic():
            create()
            raise RuntimeError("rollback")
    assert not MorningPaymentLinkDelivery.objects.exists()
    dispatch.assert_not_called()


@pytest.mark.parametrize("language", ["ar", "en", "fr"])
def test_render_is_one_message_with_exact_times_and_amount(settings, language):
    stay = stay_without_notice(settings)
    stay.guest_language = language
    body = links.render_message(stay)
    assert body.count(links.public_url(stay)) == 1
    assert "01:00" in body and "11:00" in body and "500.00" in body
    assert len(body) < 4096
    assert "Hostaway" not in body


def test_expired_eligible_stay_renews_same_link_only_after_availability(
    settings,
    django_capture_on_commit_callbacks,
):
    stay = stay_without_notice(settings)
    original_url = links.public_url(stay)
    MorningStay.objects.filter(pk=stay.pk).update(
        status="expired", expires_at=timezone.now() - timedelta(minutes=1)
    )
    request_id = uuid.uuid4()
    with patch.object(links, "verify_window", return_value={"checked_at": "synthetic"}) as check:
        with patch.object(links, "dispatch") as dispatch:
            with django_capture_on_commit_callbacks(execute=True):
                delivery = links.resend(stay.pk, request_id)
            dispatch.assert_called_once_with(delivery.pk)
        assert links.resend(stay.pk, request_id).pk == delivery.pk
        check.assert_called_once()
    stay.refresh_from_db()
    assert stay.status == "open" and stay.expires_at > timezone.now()
    assert stay.expires_at <= timezone.now() + timedelta(minutes=5)
    assert links.public_url(stay) == original_url
    assert not stay.attempts.exists() and not stay.communication_events.exists()


@pytest.mark.parametrize("status", ["paid", "processing", "review", "cancelled"])
def test_financial_or_cancelled_states_do_not_renew_or_send(settings, status):
    stay = stay_without_notice(settings)
    MorningStay.objects.filter(pk=stay.pk).update(status=status)
    with patch.object(links, "verify_window") as check, pytest.raises(MorningStayError):
        links.resend(stay.pk, uuid.uuid4())
    check.assert_not_called()
    assert not MorningPaymentLinkDelivery.objects.exists()


@pytest.mark.parametrize("status", ["created", "pending", "unknown", "review", "succeeded"])
def test_unresolved_attempt_blocks_resend_even_when_stay_is_open(settings, status):
    stay = stay_without_notice(settings)
    MorningPaymentAttempt.objects.create(
        stay=stay,
        merchant_transaction_id="synthetic-attempt",
        amount=stay.amount_sar,
        status=status,
    )
    with pytest.raises(MorningStayError):
        links.resend(stay.pk, uuid.uuid4())
    assert not MorningPaymentLinkDelivery.objects.exists()


def test_ended_stay_and_unavailable_remote_do_not_renew(settings):
    stay = stay_without_notice(settings)
    previous = stay.expires_at
    with patch.object(links, "verify_window", side_effect=MorningStayError("شاغل سابق")):
        with pytest.raises(MorningStayError):
            links.resend(stay.pk, uuid.uuid4())
    stay.refresh_from_db()
    assert stay.expires_at == previous
    MorningStay.objects.filter(pk=stay.pk).update(
        starts_at=timezone.now() - timedelta(hours=2),
        ends_at=timezone.now() - timedelta(seconds=1),
    )
    with patch.object(links, "verify_window", side_effect=MorningStayError("occupied")):
        with pytest.raises(MorningStayError):
            links.resend(stay.pk, uuid.uuid4())
    assert not MorningPaymentLinkDelivery.objects.exists()


def test_competing_morning_allocation_prevents_renewal(settings):
    stay = stay_without_notice(settings)
    MorningStay.objects.create(
        request_id=uuid.uuid4(),
        property=stay.property,
        starts_at=stay.starts_at,
        ends_at=stay.ends_at,
        amount_sar=stay.amount_sar,
        guest_name="Other Synthetic",
        guest_phone="+16502530001",
        guest_email="other@example.invalid",
        guests=1,
        environment=stay.environment,
        expires_at=stay.expires_at,
        availability_checked_at=timezone.now(),
        created_by=stay.created_by,
    )
    with patch.object(links, "verify_window", return_value={}):
        with pytest.raises(MorningStayError, match="متداخلة"):
            links.resend(stay.pk, uuid.uuid4())
    assert not stay.payment_link_deliveries.exists()


def test_accepted_send_is_once_per_intent_and_does_not_confirm_admission(settings):
    stay = stay_without_notice(settings)
    delivery = links.queue_initial(stay)
    provider = Mock()
    provider.send_image.return_value = {"sent": "true", "id": "synthetic-message"}
    with (
        patch.object(links, "verify_window", return_value={}),
        patch.object(links, "UltraMsgClient") as gateway,
    ):
        gateway.return_value.__enter__.return_value = provider
        assert links.send_link(delivery.pk) == "accepted"
        assert links.send_link(delivery.pk) == "accepted"
    provider.send_image.assert_called_once()
    args = provider.send_image.call_args.kwargs
    assert args["recipient"] == stay.guest_phone
    assert args["caption"].count(links.public_url(stay)) == 1
    assert args["reference_id"] == "morning-pay-" + str(delivery.pk)
    stay.refresh_from_db()
    assert stay.status == "open" and not stay.paid_at
    assert not stay.communication_events.exists()


def test_timeout_is_uncertain_and_task_replay_never_posts_twice(settings):
    stay = stay_without_notice(settings)
    delivery = links.queue_initial(stay)
    with (
        patch.object(links, "verify_window", return_value={}),
        patch.object(links, "UltraMsgClient") as gateway,
    ):
        provider = gateway.return_value.__enter__.return_value
        provider.send_image.side_effect = UltraMsgConnectionError()
        assert links.send_link(delivery.pk) == "uncertain"
        assert links.send_link(delivery.pk) == "uncertain"
        assert provider.send_image.call_count == 1


@pytest.mark.parametrize("change", ["expired", "paid", "phone", "unavailable"])
def test_worker_rechecks_before_sending(settings, change):
    stay = stay_without_notice(settings)
    delivery = links.queue_initial(stay)
    if change == "expired":
        MorningStay.objects.filter(pk=stay.pk).update(expires_at=timezone.now())
    elif change == "paid":
        MorningStay.objects.filter(pk=stay.pk).update(status="paid", paid_at=timezone.now())
    elif change == "phone":
        MorningStay.objects.filter(pk=stay.pk).update(guest_phone="+16502530001")
    with (
        patch.object(links, "verify_window", side_effect=MorningStayError("unavailable")),
        patch.object(links, "UltraMsgClient") as provider,
    ):
        assert links.send_link(delivery.pk) == "expired"
        provider.assert_not_called()


def test_ui_csrf_signed_intent_duplicate_click_and_later_resend(settings):
    stay = stay_without_notice(settings)
    client = Client()
    user = stay.created_by
    client.force_login(user)
    url = reverse("notifications:morning_detail", args=[stay.pk])
    page = client.get(url)
    token = page.context["send_token"]
    assert "إرسال الحجز ورابط الدفع عبر واتساب" in page.content.decode()
    strict = Client(enforce_csrf_checks=True)
    strict.force_login(user)
    assert strict.post(url, {"action": "send_payment_link", "send_token": token}).status_code == 403
    with patch.object(links, "verify_window", return_value={}):
        assert (
            client.post(url, {"action": "send_payment_link", "send_token": "forged"}).status_code
            == 302
        )
        assert not MorningPaymentLinkDelivery.objects.exists()
        values = {"action": "send_payment_link", "send_token": token}
        assert client.post(url, values).status_code == 302
        assert client.post(url, values).status_code == 302
        assert MorningPaymentLinkDelivery.objects.count() == 1
        newer = client.get(url).context["send_token"]
        assert client.post(url, {**values, "send_token": newer}).status_code == 302
        assert MorningPaymentLinkDelivery.objects.count() == 2
    assert Client().post(url, values).status_code == 302


def test_queue_failure_is_visible_not_silent(settings):
    stay = stay_without_notice(settings)
    delivery = links.queue_initial(stay)
    with patch(
        "apps.reservations.tasks.deliver_morning_payment_link.delay", side_effect=RuntimeError
    ):
        links.dispatch(delivery.pk)
    delivery.refresh_from_db()
    assert delivery.state == "failed" and delivery.last_error == "queue_unavailable"


@pytest.mark.django_db(transaction=True)
def test_postgres_parallel_tasks_send_one_message(settings):
    if connection.vendor != "postgresql":
        pytest.skip("PostgreSQL row locks required")
    stay = stay_without_notice(settings)
    with patch.object(links, "dispatch"):
        delivery = links.queue_initial(stay)
    barrier = Barrier(2)
    provider = Mock()
    provider.send_image.return_value = {"sent": "true", "id": "synthetic-one"}

    def check(*args, **kwargs):
        barrier.wait(timeout=10)
        return {}

    def run(_):
        close_old_connections()
        try:
            return links.send_link(delivery.pk)
        finally:
            close_old_connections()

    with (
        patch.object(links, "verify_window", side_effect=check),
        patch.object(links, "UltraMsgClient") as gateway,
    ):
        gateway.return_value.__enter__.return_value = provider
        with ThreadPoolExecutor(max_workers=2) as pool:
            states = list(pool.map(run, range(2)))
    assert all(state in {"sending", "accepted"} for state in states)
    assert provider.send_image.call_count == 1


def test_fingerprint_survives_database_timezone_and_decimal_normalization(settings):
    stay = stay_without_notice(settings)
    before = links.fingerprint(stay)
    stay.refresh_from_db()
    assert links.fingerprint(stay) == before


def test_started_unfinished_request_renews_sends_and_completes_payment(
    settings,
    django_capture_on_commit_callbacks,
):
    from apps.payments.morning_payments import create_checkout, require_payable, verify_payment
    from tests.test_hyperpay import HyperPayStub
    from tests.test_morning_stays import approved, consent
    from tests.test_reservation_payment_links import billing

    # A real financial callback on a renewed request must reach paid, not review.
    settings.HYPERPAY_ENVIRONMENT = "production"
    settings.MORNING07_N8N_ENABLED = True
    stay = stay_without_notice(settings)
    original_times = stay.starts_at, stay.ends_at
    original_url = links.public_url(stay)
    clock = stay.starts_at + timedelta(hours=2)
    gateway = HyperPayStub()
    with patch("django.utils.timezone.now", return_value=clock):
        # Link eligibility is independent of the temporary allocation hold.
        require_payable(stay)
        with patch.object(
            links, "verify_window", return_value={"checked_at": clock.isoformat()}
        ) as check:
            with patch.object(links, "dispatch"):
                delivery = links.resend(stay.pk, uuid.uuid4())
            assert check.call_args.kwargs["allow_started"] is True
        stay.refresh_from_db()
        assert stay.expires_at == clock + timedelta(minutes=5)
        assert (stay.starts_at, stay.ends_at) == original_times
        assert links.public_url(stay) == original_url
        require_payable(stay)
        provider = Mock()
        provider.send_image.return_value = {"sent": "true", "id": "synthetic-renewed"}
        with (
            patch.object(links, "verify_window", return_value={}) as check,
            patch.object(links, "UltraMsgClient") as client,
        ):
            client.return_value.__enter__.return_value = provider
            assert links.send_link(delivery.pk) == "accepted"
            assert check.call_args.kwargs["allow_started"] is True
        assert not stay.communication_events.exists()
        with patch("apps.payments.morning_payments.verify_window", return_value={}) as check:
            attempt = create_checkout(
                stay.pk, billing_data=billing(), acceptance=consent(), client=gateway
            )
            assert check.call_args.kwargs["allow_started"] is True
        gateway.payment_document = approved(attempt)
        with patch("apps.reservations.morning07_communications.queue_stay_event") as queue:
            with django_capture_on_commit_callbacks(execute=True):
                verify_payment(attempt.pk, client=gateway)
            queue.assert_called_once_with(str(stay.pk))
        stay.refresh_from_db()
        assert stay.status == "paid" and stay.paid_at == clock
        assert stay.communication_events.get().payload["payment_verified"] is True
        assert stay.attempts.count() == 1
        assert stay.amount_sar == 500


def test_renewal_is_capped_at_checkout_and_does_not_reopen_ended_stay(settings):
    from apps.payments.morning_payments import require_payable

    stay = stay_without_notice(settings)
    clock = stay.ends_at - timedelta(minutes=2)
    with (
        patch("django.utils.timezone.now", return_value=clock),
        patch.object(links, "verify_window", return_value={}),
        patch.object(links, "dispatch"),
    ):
        links.resend(stay.pk, uuid.uuid4())
        stay.refresh_from_db()
        assert stay.expires_at == stay.ends_at
        require_payable(stay)
    # The payment link also remains available for late settlement.
    stay.expires_at = stay.ends_at + timedelta(minutes=10)
    with patch("django.utils.timezone.now", return_value=stay.ends_at):
        require_payable(stay)
        with patch.object(links, "verify_window", return_value={}), patch.object(links, "dispatch"):
            links.resend(stay.pk, uuid.uuid4())


def test_started_renewal_runs_actual_availability_rules_without_weakening_new_creation(settings):
    from tests.test_morning_stays import property_and_remote

    property_obj, remote = property_and_remote()
    settings.ULTRAMSG_ENABLED = False
    stay = create(property_obj, remote)
    settings.ULTRAMSG_ENABLED = True
    clock = stay.starts_at + timedelta(hours=1)
    with patch("django.utils.timezone.now", return_value=clock), patch.object(links, "dispatch"):
        links.resend(stay.pk, uuid.uuid4(), client=remote)
        stay.refresh_from_db()
        assert stay.availability_evidence["source"] == "hostaway_read_only"
        assert remote.get_listing_calendar.call_count == 2
        # Existing renewal is allowed; creating a new request in the past still is not.
        with pytest.raises(MorningStayError, match="لم يبدأ"):
            create_stay(
                actor=stay.created_by,
                property_obj=property_obj,
                request_id=uuid.uuid4(),
                client=remote,
                **{
                    key: getattr(stay, key)
                    for key in (
                        "starts_at",
                        "ends_at",
                        "guest_name",
                        "guest_phone",
                        "guest_email",
                        "guest_language",
                        "guests",
                        "amount_sar",
                    )
                },
            )


def test_send_button_performs_provider_post_now_and_reports_acceptance_without_queue(settings):
    stay = stay_without_notice(settings)
    client = Client()
    client.force_login(stay.created_by)
    url = reverse("notifications:morning_detail", args=[stay.pk])
    page = client.get(url)
    values = {"action": "send_payment_link", "send_token": page.context["send_token"]}
    with (
        patch.object(links, "verify_window", return_value={}),
        patch.object(links, "UltraMsgClient") as remote,
        patch.object(links, "dispatch") as dispatch,
    ):
        provider = remote.return_value.__enter__.return_value
        provider.send_image.return_value = {"sent": "true", "id": "synthetic-live-button"}
        assert client.post(url, values).status_code == 302
        assert client.post(url, values).status_code == 302
        assert provider.send_image.call_count == 1
        dispatch.assert_not_called()
    delivery = stay.payment_link_deliveries.get()
    assert delivery.state == "accepted" and delivery.provider_id == "synthetic-live-button"
    assert "قبلت خدمة واتساب" in client.get(url).content.decode()
    assert not stay.communication_events.exists()


def test_create_send_button_is_one_explicit_branded_provider_send(settings):
    from tests.test_manual_booking_operations_phase61 import owner
    from tests.test_morning_stays import property_and_remote, window

    property_obj, source = property_and_remote()
    actor = owner()
    client = Client()
    client.force_login(actor)
    start, _ = window()
    url = reverse("notifications:morning_create")
    request_id = client.get(url).context["form"].initial["request_id"]
    data = dict(
        request_id=request_id,
        property=property_obj.pk,
        date=start.date(),
        start_time="01:00",
        end_time="11:00",
        guest_name="Synthetic Guest",
        guest_phone="+16502530000",
        guest_email="morning@example.invalid",
        guests=1,
        amount_sar="500",
        action="create_and_send",
    )
    with (
        patch("apps.reservations.morning_stays.HostawayClient") as remote,
        patch.object(links, "UltraMsgClient") as client_mock,
        patch.object(links, "dispatch") as dispatch,
    ):
        remote.return_value.__enter__.return_value = source
        provider = client_mock.return_value.__enter__.return_value
        provider.send_image.return_value = {"sent": "true", "id": "synthetic-create-send"}
        assert client.post(url, data).status_code == 302
        assert client.post(url, data).status_code == 302
        assert provider.send_image.call_count == 1
        dispatch.assert_not_called()
    stay = MorningStay.objects.get()
    assert stay.payment_link_deliveries.get().state == "accepted"
    assert not stay.communication_events.exists()


@pytest.mark.parametrize("language", ["ar", "en", "fr"])
def test_branded_caption_fits_provider_at_maximum_names(settings, language):
    stay = stay_without_notice(settings)
    stay.guest_name = "ا" * 200
    stay.guest_language = language
    setattr(stay.property, "name_" + language, "A" * 255)
    settings.SITE_BASE_URL = "https://luxurysmartapartments.com"
    assert len(links.render_message(stay).encode("utf-16-le")) // 2 <= 1024
    assert links.notice_image(language).startswith(b"\x89PNG")
