"""Fictional identities and mocked Hostaway only; never query live guests."""

import uuid
from datetime import timedelta
from decimal import Decimal
from unittest.mock import MagicMock, patch

import httpx
import pytest
from django.contrib.auth import get_user_model
from django.db.models.query import QuerySet
from django.test import Client
from django.utils import timezone

from apps.accounts.booking_history import (
    canonical_phone,
    run_page,
    start_import,
)
from apps.accounts.history_copy import COPY
from apps.accounts.models import (
    BookingHistoryImport,
    GuestBookingHistory,
    VerifiedPhone,
    profile_for,
)
from apps.integrations.hostaway.client import HostawayClient
from apps.loyalty.models import Ledger
from apps.reservations.models import BookingIntent, Reservation
from tests.test_account_booking_claim import client_holding, make_reservation

pytestmark = pytest.mark.django_db
EMAIL = "history@example.invalid"
PHONE = "+12025550123"


@pytest.fixture
def guest():
    user = get_user_model().objects.create_user(username=EMAIL, email=EMAIL, first_name="ضيف")
    profile_for(user).mark_verified()
    VerifiedPhone.objects.create(user=user, phone=PHONE)
    client = Client()
    client.force_login(user)
    return user, client


@pytest.fixture(autouse=True)
def no_dispatch():
    from apps.accounts.booking_history import dispatch as original

    with patch("apps.accounts.booking_history.dispatch") as dispatch:
        dispatch.actual = original
        yield dispatch


def booking(reference="HISTORY-LOCAL", *, email=EMAIL, phone=PHONE, hostaway_id=None):
    record = make_reservation(reference)
    intent = record.booking_intent
    intent.guest_email, intent.guest_phone = email, phone
    intent.save(update_fields=["guest_email", "guest_phone"])
    if hostaway_id:
        record.hostaway_reservation_id = hostaway_id
        record.save(update_fields=["hostaway_reservation_id"])
    return record


def remote(identifier=990001, **overrides):
    return {
        "id": identifier,
        "listingMapId": 77777,
        "guestEmail": EMAIL,
        "phone": "0012025550123",
        "arrivalDate": "2026-01-10",
        "departureDate": "2026-01-12",
        "numberOfGuests": 2,
        "currency": "SAR",
        "totalPrice": 750,
        "status": "confirmed",
        "channelId": 2005,
        "channelName": "Booking.com",
        "listingName": "إقامة افتراضية",
        **overrides,
    }


def run(job, rows):
    factory = MagicMock()
    factory.return_value.__enter__.return_value.account_booking_history_page.return_value = rows
    result = run_page(str(job.pk), str(job.generation), client_factory=factory)
    job.refresh_from_db()
    return result, factory.return_value.__enter__.return_value


def test_one_click_claims_only_exact_local_pair_and_never_posts_rewards(guest):
    user, client = guest
    own = booking()
    wrong_phone = booking("HISTORY-PHONE", phone="+442079460123")
    wrong_email = booking("HISTORY-EMAIL", email="other@example.invalid")
    with patch("apps.loyalty.site_adapter.schedule_reconciliation") as rewards:
        response = client.post("/my-bookings/restore/", {"email": "other@example.invalid"})
        assert response.status_code == 302
        assert BookingIntent.objects.get(pk=own.booking_intent_id).customer_id == user.pk
        assert BookingIntent.objects.get(pk=wrong_phone.booking_intent_id).customer_id is None
        assert BookingIntent.objects.get(pk=wrong_email.booking_intent_id).customer_id is None
        rewards.assert_not_called()
        assert not Ledger.objects.exists()


@pytest.mark.parametrize("missing", ["email", "phone", "saved_phone_only", "inactive"])
def test_both_verified_credentials_are_required_before_queueing_or_linking(
    guest, no_dispatch, missing
):
    user, client = guest
    record = booking()
    if missing == "email":
        profile = profile_for(user)
        profile.email_verified_at = None
        profile.save()
    elif missing == "inactive":
        user.is_active = False
        user.save()
    else:
        VerifiedPhone.objects.filter(user=user).delete()
        if missing == "saved_phone_only":
            profile = profile_for(user)
            profile.phone = PHONE
            profile.save()
    client.post("/my-bookings/restore/")
    assert not BookingHistoryImport.objects.exists()
    assert BookingIntent.objects.get(pk=record.booking_intent_id).customer_id is None
    no_dispatch.assert_not_called()


def test_restore_is_post_only_login_required_and_csrf_protected(guest):
    _, client = guest
    assert Client().post("/my-bookings/restore/").status_code == 302
    assert client.get("/my-bookings/restore/").status_code == 405
    strict = Client(enforce_csrf_checks=True)
    strict.force_login(guest[0])
    assert strict.post("/my-bookings/restore/").status_code == 403
    assert not BookingHistoryImport.objects.exists()


def test_repeat_click_and_duplicate_task_do_not_duplicate_records(guest):
    user, _ = guest
    job, state = start_import(user)
    assert state == "started"
    assert start_import(user)[1] == "running"
    assert run(job, [remote()])[0] == "queued"
    assert run(job, [])[0] == "complete"
    assert run(job, [remote(990002)])[0] == "obsolete"
    assert GuestBookingHistory.objects.count() == 1
    assert job.added == 1
    assert start_import(user)[1] == "cooldown"


def test_paginated_history_includes_all_dates_and_cancelled_with_and_matching(guest):
    user, client = guest
    job, _ = start_import(user)
    result, provider = run(
        job,
        [
            remote(),
            remote(990002, guestEmail="someone@example.invalid"),
            remote(990003, phone="+442079460123"),
            remote(990004, guestEmail=None),
        ],
    )
    assert result == "queued"  # A short page is not proof the cursor is exhausted.
    provider.account_booking_history_page.assert_called_once_with(email=EMAIL, after_id=None)
    run(
        job,
        [
            remote(990005, status="cancelled"),
            remote(990006, arrivalDate="2027-01-10", departureDate="2027-01-12"),
        ],
    )
    run(job, [])
    assert job.status == "complete" and job.added == 3
    assert set(GuestBookingHistory.objects.values_list("hostaway_reservation_id", flat=True)) == {
        990001,
        990005,
        990006,
    }
    page = client.get("/my-bookings/")
    assert page.context["booking_counts"] == {"upcoming": 1, "past": 1, "cancelled": 1}
    assert "إقامة افتراضية" in client.get("/my-bookings/?filter=past").content.decode()
    assert "990005" not in client.get("/my-bookings/?filter=past").content.decode()
    assert Reservation.objects.count() == 0 and BookingIntent.objects.count() == 0
    assert not Ledger.objects.exists()


def test_external_projection_has_no_payment_or_management_actions_and_no_contact_secrets(guest):
    user, client = guest
    job, _ = start_import(user)
    row = remote(
        guestName="Never store this",
        doorCode="secret-not-for-history",
        guestEmail="HISTORY@EXAMPLE.INVALID",
    )
    run(job, [row])
    run(job, [])
    html = client.get("/my-bookings/?filter=past").content.decode()
    assert "Booking.com" in html and "حجز مسترجع للعرض فقط" in html
    assert "Never store this" not in html and "secret-not-for-history" not in html
    assert "/reservations/manage/" not in html
    stored = GuestBookingHistory.objects.get()
    assert not hasattr(stored, "guest_email") and stored.channel_id == 2005
    assert stored.total_price == Decimal("750")


def test_site_and_remote_same_id_show_once_without_overwriting_payment(guest):
    user, client = guest
    site = booking(hostaway_id=990001)
    site.payment_status = "paid"
    site.save(update_fields=["payment_status"])
    job, _ = start_import(user)
    run(job, [remote(totalPrice=400, status="cancelled")])
    run(job, [])
    site.refresh_from_db()
    assert GuestBookingHistory.objects.count() == 0
    assert site.payment_status == "paid" and site.total_price == Decimal("500")
    assert site.normalized_status != "cancelled"
    page = client.get("/my-bookings/")
    assert sum(page.context["booking_counts"].values()) == 1
    assert len(page.context["booking_page"]) == 1


def test_never_move_a_site_or_history_record_owned_by_another_account(guest):
    user, client = guest
    other = get_user_model().objects.create_user(username="other", email="other@example.invalid")
    site = booking(hostaway_id=990001)
    site.booking_intent.customer = other
    site.booking_intent.save()
    job, _ = start_import(user)
    run(job, [remote(), remote(990002)])
    GuestBookingHistory.objects.filter(hostaway_reservation_id=990002).update(user=other)
    run(job, [remote(990002), remote(990003, guestEmail="unrelated@example.invalid")])
    run(job, [])
    assert job.conflicts == 2
    assert BookingIntent.objects.get(pk=site.booking_intent_id).customer_id == other.pk
    assert not client.get("/my-bookings/?filter=past").context["booking_page"].object_list


def test_pending_job_uses_original_identity_only_and_rechecks_before_save(guest):
    user, _ = guest
    job, _ = start_import(user)
    factory = MagicMock()

    def changed(**kwargs):
        VerifiedPhone.objects.filter(user=user).update(version=uuid.uuid4())
        return [remote()]

    factory.return_value.__enter__.return_value.account_booking_history_page.side_effect = changed
    assert run_page(str(job.pk), str(job.generation), client_factory=factory) == "partial"
    assert not GuestBookingHistory.objects.exists()


def test_failed_partial_and_looping_provider_never_report_no_matches_or_complete(guest):
    user, client = guest
    job, _ = start_import(user)
    run(job, [remote()])
    assert run(job, [remote()])[0] == "partial"
    assert GuestBookingHistory.objects.count() == 1
    html = client.get("/my-bookings/").content.decode()
    assert COPY["ar"]["partial"] in html
    assert job.status != "complete"


def test_changed_credentials_before_task_and_old_generation_cannot_import(guest):
    user, _ = guest
    job, _ = start_import(user)
    assert run_page(job.pk, uuid.uuid4()) == "obsolete"
    VerifiedPhone.objects.filter(user=user).delete()
    factory = MagicMock()
    assert run_page(job.pk, job.generation, client_factory=factory) == "partial"
    factory.assert_not_called()


def test_queue_failure_is_explicit_and_does_not_send_inline(
    guest, no_dispatch, django_capture_on_commit_callbacks
):
    user, _ = guest
    with patch(
        "apps.accounts.tasks.import_booking_history_page.apply_async",
        side_effect=RuntimeError("private broker"),
    ):
        with django_capture_on_commit_callbacks(execute=True):
            job, _ = start_import(user)
        no_dispatch.actual(job.pk, job.generation)
    job.refresh_from_db()
    assert job.status == "partial" and job.error_code == "queue_unavailable"
    assert not GuestBookingHistory.objects.exists()


def test_status_is_account_scoped_and_private(guest):
    user, client = guest
    job, _ = start_import(user)
    response = client.get("/my-bookings/restore/status/?user=other&job=forged")
    assert response.json()["running"] is True
    assert EMAIL not in response.content.decode() and PHONE not in response.content.decode()
    assert str(job.pk) not in response.content.decode()
    assert "no-store" in response["Cache-Control"]
    other = Client()
    second = get_user_model().objects.create_user(username="second")
    other.force_login(second)
    assert other.get("/my-bookings/restore/status/").json()["added"] == 0
    assert Client().get("/my-bookings/restore/status/").status_code == 302


def test_a_new_import_refreshes_old_records_and_hides_no_longer_matching_records(guest):
    user, _ = guest
    job, _ = start_import(user)
    run(job, [remote(), remote(990002)])
    run(job, [])
    BookingHistoryImport.objects.filter(pk=job.pk).update(
        requested_at=timezone.now() - timedelta(minutes=2)
    )
    job, _ = start_import(user)
    run(job, [remote(status="cancelled"), remote(990002, phone="+442079460123")])
    run(job, [])
    assert (
        GuestBookingHistory.objects.get(hostaway_reservation_id=990001).normalized_status
        == "cancelled"
    )
    assert GuestBookingHistory.objects.get(hostaway_reservation_id=990002).is_visible is False
    assert job.added == 0


@pytest.mark.parametrize("language", ["ar", "en", "fr"])
def test_recovery_copy_and_native_form_work_in_each_guest_language(guest, language):
    _, client = guest
    client.cookies["django_language"] = language
    html = client.get("/my-bookings/").content.decode()
    assert COPY[language]["button"] in html and COPY[language]["different"] in html
    assert 'action="/my-bookings/restore/"' in html
    assert 'name="csrfmiddlewaretoken"' in html
    assert "booking-history.js" in html


def test_client_uses_get_only_all_dates_cursor_and_revalidates_provider_schema():
    seen = []

    def handler(request):
        seen.append(request)
        assert request.method == "GET" and request.url.path == "/v1/reservations"
        return httpx.Response(200, json={"status": "success", "result": [remote()]})

    with httpx.Client(
        base_url="https://api.hostaway.com/v1", transport=httpx.MockTransport(handler)
    ) as transport:
        with HostawayClient(client=transport, access_token="fictional-only") as client:
            assert len(client.account_booking_history_page(email=EMAIL, after_id=999)) == 1
    params = dict(seen[0].url.params)
    assert params == {
        "limit": "100",
        "includeResources": "0",
        "guestEmail": EMAIL,
        "afterId": "999",
    }


@pytest.mark.parametrize(
    "phone", ["+1 (202) 555-0123", "0012025550123", "12025550123", "＋１２０２５５５０１２３"]
)
def test_only_full_valid_international_numbers_are_normalized(phone):
    assert canonical_phone(phone) == PHONE


@pytest.mark.parametrize("phone", ["2025550123", "5550123", "+12025550123 ext 1", None, "***0123"])
def test_incomplete_masked_and_ambiguous_phone_numbers_do_not_match(phone):
    assert canonical_phone(phone) is None


def test_bad_matched_row_does_not_discard_safe_matches_and_needs_review(guest):
    user, client = guest
    job, _ = start_import(user)
    run(job, [remote(totalPrice=-1), remote(990002)])
    run(job, [])
    assert job.skipped == 1 and job.added == 1 and job.status == "complete"
    assert GuestBookingHistory.objects.get().hostaway_reservation_id == 990002
    assert COPY["ar"]["review"] in client.get("/my-bookings/").content.decode()


def test_active_lease_never_issues_another_provider_request(guest):
    user, _ = guest
    job, _ = start_import(user)
    BookingHistoryImport.objects.filter(pk=job.pk).update(
        locked_until=timezone.now() + timedelta(seconds=30), lease=uuid.uuid4()
    )
    factory = MagicMock()
    assert run_page(job.pk, job.generation, client_factory=factory) == "busy"
    factory.assert_not_called()


def test_stale_search_can_restart_but_the_old_inflight_page_cannot_commit(guest):
    user, _ = guest
    job, _ = start_import(user)
    old_generation = job.generation
    factory = MagicMock()

    def restart(**kwargs):
        BookingHistoryImport.objects.filter(pk=job.pk).update(
            updated_at=timezone.now() - timedelta(minutes=6),
            requested_at=timezone.now() - timedelta(minutes=6),
        )
        new_job, state = start_import(user)
        assert state == "started" and new_job.generation != old_generation
        return [remote()]

    factory.return_value.__enter__.return_value.account_booking_history_page.side_effect = restart
    assert run_page(job.pk, old_generation, client_factory=factory) == "obsolete"
    assert not GuestBookingHistory.objects.exists()
    job.refresh_from_db()
    assert job.status == "queued" and job.pages == 0


def test_site_card_hides_an_older_remote_projection_for_the_same_booking(guest):
    user, client = guest
    job, _ = start_import(user)
    run(job, [remote()])
    run(job, [])
    site = booking(hostaway_id=990001)
    site.booking_intent.customer = user
    site.booking_intent.save(update_fields=["customer"])
    page = client.get("/my-bookings/")
    assert sum(page.context["booking_counts"].values()) == 1
    assert len(page.context["booking_page"]) == 1
    assert not getattr(page.context["booking_page"][0], "history_only", False)


def test_native_button_accepts_valid_csrf_and_uses_only_session_identity(guest):
    user, _ = guest
    strict = Client(enforce_csrf_checks=True)
    strict.force_login(user)
    strict.get("/my-bookings/")
    response = strict.post(
        "/my-bookings/restore/",
        {"csrfmiddlewaretoken": strict.cookies["csrftoken"].value, "user": "forged"},
    )
    assert response.status_code == 302
    assert BookingHistoryImport.objects.get().user_id == user.pk


def test_original_session_claim_cannot_overwrite_a_concurrent_history_link(guest):
    from apps.accounts.services import claim_reservation

    user, _ = guest
    other = get_user_model().objects.create_user(username="other-owner")
    record = booking()
    request = client_holding(record).request().wsgi_request
    original_update = QuerySet.update

    def attach_between_read_and_write(queryset, **kwargs):
        if queryset.model is BookingIntent and kwargs.get("customer") == user:
            original_update(
                BookingIntent.objects.filter(pk=record.booking_intent_id), customer=other
            )
        return original_update(queryset, **kwargs)

    with (
        patch.object(QuerySet, "update", attach_between_read_and_write),
        patch("apps.loyalty.site_adapter.schedule_reconciliation") as rewards,
    ):
        assert claim_reservation(request, user, record.public_reference) is None
        rewards.assert_not_called()
    assert BookingIntent.objects.get(pk=record.booking_intent_id).customer_id == other.pk
