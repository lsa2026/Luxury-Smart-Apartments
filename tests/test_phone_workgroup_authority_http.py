"""Authenticated source HTTP and one-use readback, synthetic identities only."""

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
from django.db import connection, connections
from django.test import Client

from apps.accounts.models import PhoneChallenge
from apps.reservations import telegram_authority as authority
from apps.reservations import telegram_website_workgroup as transport
from tests import test_loyalty_invitation_api as invitation
from tests import test_phone_workgroup as source

pytestmark = pytest.mark.django_db
isolated = source.isolated
guest = source.guest
manual = source.manual
credential = invitation.auth
PATH = "/integrations/telegram39/activation-authority/"


def prepare(guest, callbacks, monkeypatch):
    monkeypatch.setattr(transport, "call", lambda *args: None)
    challenge = source.begin(guest, callbacks)
    source.auth.deliver_phone_code(challenge.pk)
    return challenge, authority.mint(
        {"kind": "phone_code_handoff", "job": str(challenge.pk)},
        "phone_code_handoff",
        str(challenge.pk),
    )


def post(envelope, *, authenticated=True):
    return Client(enforce_csrf_checks=True).post(
        PATH,
        dict(envelope, contract="tg39_activation_readback_v1"),
        content_type="application/json",
        secure=True,
        **(invitation.HEADERS if authenticated else {}),
    )


def test_private_readback_auth_once_and_status_never_returns_code(
    guest, monkeypatch, django_capture_on_commit_callbacks
):
    challenge, envelope = prepare(guest, django_capture_on_commit_callbacks, monkeypatch)
    assert post(envelope, authenticated=False).status_code == 401
    assert post(dict(envelope, action="website_handoff")).status_code == 403
    challenge.refresh_from_db()
    assert not challenge.workgroup_authorized_at
    response = post(envelope)
    assert response.status_code == 200
    assert "no-store" in response["Cache-Control"]
    assert response["Referrer-Policy"] == "no-referrer"
    assert response.json()["packet"]["contract"] == "tg39_phone_code_packet_v1"
    assert "caption_html" in response.json()["packet"]
    assert post(envelope).status_code == 403
    status = authority.mint(
        {"kind": "phone_code_handoff", "job": str(challenge.pk)},
        "phone_code_handoff_status",
        str(challenge.pk),
    )
    result = post(status)
    assert result.status_code == 200
    assert "caption_html" not in result.json()["packet"]
    assert "guest_phone_e164" not in result.json()["packet"]


@pytest.mark.django_db(transaction=True)
def test_concurrent_private_source_requests_release_exactly_once(
    guest, monkeypatch, django_capture_on_commit_callbacks
):
    if connection.vendor != "postgresql":
        pytest.skip("One-use source race needs isolated PostgreSQL row locks")
    challenge, envelope = prepare(guest, django_capture_on_commit_callbacks, monkeypatch)
    barrier = Barrier(2)

    def readback():
        try:
            connections.close_all()
            barrier.wait(timeout=10)
            return post(envelope).status_code
        finally:
            connections.close_all()

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _: readback(), range(2)))
    assert sorted(results) == [200, 403]
    assert PhoneChallenge.objects.get(pk=challenge.pk).workgroup_authorized_at
