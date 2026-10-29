"""Synthetic coordination tests; no real n8n, member or provider operation."""

import hashlib
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier

import pytest
from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.db import close_old_connections, connection
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import VerifiedPhone
from apps.loyalty import booking_invitations
from apps.loyalty.models import InvitationAPICredential, JoinInvitation, Ledger, Member
from tests import test_loyalty_booking_invitations as manual
from tests.test_loyalty_stage8 import join, user

TOKEN = "ab" * 32
HEADERS = {"HTTP_AUTHORIZATION": "Bearer " + TOKEN}

pytestmark = pytest.mark.django_db
rig = manual.rig
ROOT = "/integrations/loyalty/invitations/"


@pytest.fixture(autouse=True)
def auth():
    cache.clear()
    InvitationAPICredential.objects.create(
        key_sha256=hashlib.sha256(TOKEN.encode()).hexdigest(),
        created_by=user("service-owner"),
        expires_at=timezone.now() + timedelta(days=1),
    )
    yield


def post(action, data, **kwargs):
    return Client(enforce_csrf_checks=True).post(
        ROOT + action + "/",
        data,
        content_type="application/json",
        secure=True,
        **(HEADERS | kwargs),
    )


def data(**changes):
    return (
        dict(
            reservation_key="70012345",
            phone="+12025550123",
            email="sample@example.invalid",
            language="ar",
            guest_name="Synthetic Guest",
            source_platform="airbnb",
            event_id="booking-ready:70012345",
        )
        | changes
    )


def claim(**changes):
    result = post("claim", data(**changes))
    assert result.status_code == 200, result.content
    return result.json()


def preflight(result, phone="+12025550123", **changes):
    return post(
        "preflight",
        dict(invitation_id=result["invitation_id"], ticket=result["ticket"], phone=phone) | changes,
    )


def report(result, state, provider_id="sample-provider-id"):
    return post(
        "record",
        dict(
            invitation_id=result["invitation_id"],
            status=state,
            provider_reference=result["provider_reference"],
            provider_id=provider_id,
        ),
    )


@pytest.mark.parametrize("language", ["ar", "en", "fr"])
def test_claim_is_not_send_and_preflight_is_once_only(rig, language):
    _, _, provider, _ = rig
    first = claim(language=language)
    assert first["decision"] == "eligible" and first["send_allowed"] is False
    assert claim(language=language)["invitation_id"] == first["invitation_id"]
    assert preflight(first).json()["send_allowed"] is True
    assert preflight(first).json()["send_allowed"] is False
    assert claim(language=language)["decision"] == "duplicate"
    assert JoinInvitation.objects.count() == 1
    assert not Member.objects.exists() and not Ledger.objects.exists()
    assert not provider.send_image.called


def test_missing_phone_then_corrected_number_is_eligible(rig):
    assert claim(phone="")["decision"] == "phone_missing"
    assert JoinInvitation.objects.count() == 0
    result = claim(phone="0542800325")
    assert result["recipient_phone"] == "+966542800325"


@pytest.mark.parametrize("match", ["phone", "email"])
def test_existing_member_is_suppressed_without_leaking_identity(rig, match):
    account = user("suppressed")
    account.email = "sample@example.invalid" if match == "email" else "other@example.invalid"
    account.save()
    join(account)
    if match == "phone":
        VerifiedPhone.objects.create(user=account, phone="+12025550123")
    result = claim()
    assert result["decision"] == "member" and "member_number" not in result
    assert JoinInvitation.objects.count() == 0


def test_join_between_claim_and_preflight_stops_provider_permission(rig):
    result = claim()
    account = user("new-member")
    account.email = "sample@example.invalid"
    account.save()
    join(account)
    check = preflight(result).json()
    assert check["decision"] == "member" and check["send_allowed"] is False
    assert JoinInvitation.objects.get().state == "blocked"


@pytest.mark.parametrize("status", ["unknown", "accepted", "delivered", "read", "failed"])
def test_provider_states_and_corrected_phone_never_create_a_second_post(rig, status):
    result = claim()
    assert report(result, status).status_code == 409  # No send permit consumed yet.
    preflight(result)
    assert report(result, status).status_code == 200
    corrected = claim(phone="+12025550124", event_id="corrected:70012345")
    assert corrected["decision"] == "duplicate"
    assert corrected["invitation_id"] == result["invitation_id"]
    assert corrected["provider_reference"] == result["provider_reference"]
    by_booking = post("status", {"reservation_key": "70012345"}).json()
    assert by_booking["status"] == status and by_booking["send_allowed"] is False
    assert post("status", {"invitation_id": result["invitation_id"]}).json()["status"] == status
    assert JoinInvitation.objects.count() == 1


def test_unknown_preflight_response_is_reconciled_not_reauthorized(rig):
    result = claim()
    preflight(result)  # Simulated lost response: caller does NOT post to UltraMsg.
    state = post("status", {"reservation_key": "70012345"}).json()
    assert state["status"] == "unknown" and state["retry_provider_post"] is False
    assert preflight(result).json()["send_allowed"] is False


def test_ticket_bound_to_phone_and_expiry(rig):
    result = claim()
    assert preflight(result, phone="+12025550124").status_code == 409
    assert preflight(result, ticket="tampered").status_code == 409
    JoinInvitation.objects.update(expires_at=timezone.now() - timedelta(seconds=1))
    assert preflight(result).status_code == 409
    renewed = claim(phone="+12025550124", event_id="corrected-before-send")
    assert renewed["invitation_id"] == result["invitation_id"]
    assert preflight(result).status_code == 409
    assert preflight(renewed, phone="+12025550124").json()["send_allowed"] is True


def test_delivery_order_and_provider_conflict_do_not_corrupt_evidence(rig):
    result = claim()
    preflight(result)
    assert report(result, "read").json()["status"] == "read"
    assert report(result, "accepted").json()["status"] == "read"
    assert report(result, "failed").json()["status"] == "read"
    assert report(result, "delivered", "different-id").status_code == 409
    assert report(result, "accepted", "").status_code == 409
    assert len(JoinInvitation.objects.get().outcomes) == 3


def test_same_phone_or_email_and_event_conflict_are_suppressed(rig):
    claim()
    assert claim(reservation_key="70012346", event_id="another")["decision"] == "duplicate"
    assert (
        claim(reservation_key="70012346", phone="+12025550124", event_id="another")["decision"]
        == "duplicate"
    )
    conflict = post("claim", data(reservation_key="70012346"))
    assert conflict.status_code == 409


def test_shared_manual_booking_claim_blocks_n8n_across_channels(rig):
    client, _, _, _ = rig
    booking = manual.past_booking()
    booking.source_type = "external_channel"
    booking.save()
    client.post(manual.url(booking), manual.payload(client, booking, "email"))
    result = claim(reservation_key=str(booking.hostaway_reservation_id))
    assert result["decision"] == "duplicate"
    manual.deliver_latest(booking, "email")
    assert claim()["decision"] == "duplicate"  # Same phone on another booking.


def test_n8n_claim_blocks_manual_email_and_whatsapp(rig):
    client, _, provider, dispatched = rig
    booking = manual.past_booking()
    booking.source_type = "external_channel"
    booking.save()
    claim(reservation_key=str(booking.hostaway_reservation_id))
    for channel in ("email", "whatsapp"):
        request = Client().post("/").wsgi_request
        request.user = client.get(manual.url(booking)).wsgi_request.user
        request.POST = manual.payload(client, booking, channel)
        with pytest.raises(ValidationError):
            booking_invitations.queue_booking_invitation(request, booking.pk)
    assert not provider.send_image.called and not dispatched


def test_legacy_manual_receipt_is_retained_and_prevents_n8n(rig):
    booking = manual.past_booking()
    booking.source_type = "external_channel"
    booking.save()
    legacy = {"status": "uncertain", "requested_at": timezone.now().isoformat()}
    booking.loyalty_state[booking_invitations.EMAIL_STATE] = legacy.copy()
    booking.save()
    assert claim(reservation_key=str(booking.hostaway_reservation_id))["decision"] == "duplicate"
    booking.refresh_from_db()
    assert booking.loyalty_state[booking_invitations.EMAIL_STATE] == legacy


@pytest.mark.parametrize("action", ["claim", "preflight", "record", "status"])
def test_auth_https_and_bad_payload_fail_closed(rig, action):
    url = ROOT + action + "/"
    assert Client().post(url, {}, content_type="application/json", secure=True).status_code == 401
    assert Client().post(url, {}, content_type="application/json", **HEADERS).status_code == 403
    assert post(action, {"unexpected": "value"}).status_code == 400
    assert not JoinInvitation.objects.exists()


def test_programme_gate_health_and_owner_readiness(rig, settings):
    client, _, _, _ = rig
    url = reverse("notifications:loyalty_invitation_integration")
    assert client.get(url).json()["authentication_configured"] is True
    assert Client().get(url).status_code != 200
    assert Client().get(ROOT + "health/", secure=True, **HEADERS).json()["code"] == "ready"
    settings.LOYALTY_PILOT_ONLY = True
    assert post("claim", data()).status_code == 503


def test_dedicated_credential_owner_setup_single_download_csrf_and_revoke(rig):
    import hashlib

    client, owner, _, _ = rig
    url = reverse("notifications:loyalty_invitation_credential")
    page = client.get(url)
    proof = page.context["confirmation"]
    assert "Bearer " not in page.content.decode()
    strict = Client(enforce_csrf_checks=True)
    strict.force_login(owner)
    assert strict.post(url, {"confirmation": proof}).status_code == 403
    result = client.post(url, {"confirmation": proof})
    assert result.status_code == 200 and "no-store" in result["Cache-Control"]
    credential = result.json()
    value = credential["data"]["value"]
    saved = InvitationAPICredential.objects.get(pk=credential["website_credential_id"])
    assert saved.key_sha256 == hashlib.sha256(value[7:].encode()).hexdigest()
    assert value[7:] not in saved.key_sha256
    assert client.post(url, {"confirmation": proof}).status_code == 409
    assert Client().get(ROOT + "health/", secure=True, HTTP_AUTHORIZATION=value).status_code == 200
    page = client.get(url)
    revoke = next(
        row["confirmation"] for row in page.context["credentials"] if row["id"] == str(saved.pk)
    )
    assert client.post(url, {"confirmation": revoke}).json()["code"] == "revoked"
    assert Client().get(ROOT + "health/", secure=True, HTTP_AUTHORIZATION=value).status_code == 401
    assert Client().get(url).status_code != 200


def test_ticket_cannot_be_used_for_a_different_claim_and_reference(rig):
    first = claim()
    other = claim(
        reservation_key="70022345",
        phone="+12025550124",
        email="different@example.invalid",
        event_id="different",
    )
    assert (
        preflight(first, invitation_id=other["invitation_id"], phone="+12025550124").status_code
        == 409
    )
    preflight(first)
    bad = post(
        "record",
        {
            "invitation_id": first["invitation_id"],
            "provider_reference": other["provider_reference"],
            "status": "accepted",
            "provider_id": "sample",
        },
    )
    assert bad.status_code == 409


def test_service_auth_does_not_accept_unrelated_sama_token(rig, settings):
    from tests.test_sama_booking_api import TOKEN as SAMA_TOKEN

    settings.SAMA_BOOKING_ENABLED = True
    settings.SAMA_BOOKING_KEY_SHA256 = hashlib.sha256(SAMA_TOKEN.encode()).hexdigest()
    # Revoke the synthetic invitation key: sharing a credential value is never inferred.
    InvitationAPICredential.objects.update(revoked_at=timezone.now())
    assert (
        Client()
        .get(ROOT + "health/", secure=True, HTTP_AUTHORIZATION="Bearer " + SAMA_TOKEN)
        .status_code
        == 401
    )


@pytest.mark.django_db(transaction=True)
def test_postgres_concurrent_claims_have_one_winner(rig):
    if connection.vendor != "postgresql":
        pytest.skip("Requires independent PostgreSQL advisory locks.")
    barrier = Barrier(2)

    def run(index):
        close_old_connections()
        try:
            barrier.wait(timeout=10)
            return claim(reservation_key=str(70012345 + index), event_id="event:" + str(index))[
                "decision"
            ]
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(run, range(2)))
    assert sorted(results) == ["duplicate", "eligible"]
    assert JoinInvitation.objects.count() == 1


def test_rechecking_cancelled_booking_cannot_release_consumed_invitation(rig):
    booking = manual.past_booking()
    booking.source_type = "external_channel"
    booking.save()
    first = claim(reservation_key=str(booking.hostaway_reservation_id))
    assert preflight(first).json()["send_allowed"] is True
    booking.normalized_status = "cancelled"
    booking.save()
    assert preflight(first).json()["send_allowed"] is False
    booking.normalized_status = "confirmed"
    booking.save()
    assert (
        claim(reservation_key=str(booking.hostaway_reservation_id), phone="+12025550124")[
            "decision"
        ]
        == "duplicate"
    )


def test_expired_same_event_can_refresh_only_unconsumed_claim(rig):
    first = claim()
    row = JoinInvitation.objects.get(pk=first["invitation_id"])
    row.expires_at = timezone.now() - timedelta(seconds=1)
    row.save()
    second = claim()
    assert first["invitation_id"] == second["invitation_id"]
    assert preflight(first).status_code == 409
    assert preflight(second).json()["send_allowed"] is True
