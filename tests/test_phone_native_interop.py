"""Actual Django source JSON to frozen Native39 JS; stdin/heap only, no provider."""

import hashlib
import json
import subprocess
from pathlib import Path

import pytest

from apps.accounts import phone_workgroup as service
from apps.accounts.models import PhoneChallenge
from apps.reservations import telegram_website_workgroup as transport
from tests import test_phone_workgroup as source

pytestmark = pytest.mark.django_db
isolated = source.isolated
guest = source.guest
manual = source.manual
ROOT = Path(__file__).parent
NATIVE_SHA = "9c1421ea4b529026d397a833b9d5673116ef909a0631f9026a7fbe0ea0b10b4d"


@pytest.mark.parametrize("language", ["ar", "en", "fr"])
@pytest.mark.parametrize("purpose", ["bind", "login"])
@pytest.mark.parametrize("phone", ["+966501234567", "+6835123"])
def test_actual_source_bytes_validate_and_split_in_native_without_persisting_code(
    language, purpose, phone, guest, monkeypatch, isolated, django_capture_on_commit_callbacks
):
    # The seven-digit E.164 case uses libphonenumber's NU four-digit mobile pattern.
    from django.test import Client

    from apps.accounts.models import VerifiedPhone
    from apps.core.phone_numbers import normalize_phone_number

    assert normalize_phone_number(phone) == phone
    if purpose == "login":
        VerifiedPhone.objects.create(user=guest.user, phone=phone)
        client = Client()
    else:
        client = guest.client
    response = source.legacy.start(
        client,
        django_capture_on_commit_callbacks,
        binding=purpose == "bind",
        phone=phone,
    )
    assert response.status_code == 302
    challenge = PhoneChallenge.objects.get(pk=client.session[source.auth.PENDING])
    challenge.language = language
    challenge.save(update_fields=["language"])
    monkeypatch.setattr(transport, "call", lambda *args: None)
    source.auth.deliver_phone_code(challenge.pk)
    challenge.refresh_from_db()
    response = service.authorize(
        {"kind": "phone_code_handoff", "job": str(challenge.pk)},
        "phone_code_handoff",
        str(challenge.pk),
        {},
    )
    packet = response["packet"]
    payload = {
        "packet": packet,
        "dispatch": True,
        "now": int(isolated.clock[0].timestamp() * 1000),
        "synthetic_code": service.code_for(challenge),
    }
    fixture = ROOT / "js/tg39-phone-protocol.fixture.mjs"
    assert hashlib.sha256(fixture.read_bytes()).hexdigest() == NATIVE_SHA
    result = subprocess.run(
        ["node", str(ROOT / "js/check-phone-source.mjs")],
        input=json.dumps(payload, ensure_ascii=False).encode("utf8"),
        capture_output=True,
        check=False,
        timeout=10,
    )
    assert result.returncode == 0, result.stdout.decode("utf8")
    checked = json.loads(result.stdout)
    assert checked["valid"] and checked["code_only_in_text"]
    assert checked["nonce_not_in_scope_or_parts"]
    assert checked["methods"] == ["photo", "text"]
    assert len(checked["scope_fields"]) == 14 and len(set(checked["part_hashes"])) == 2
    status = service.authorize(
        {"kind": "phone_code_handoff", "job": str(challenge.pk)},
        "phone_code_handoff_status",
        str(challenge.pk),
        {},
    )
    result = subprocess.run(
        ["node", str(ROOT / "js/check-phone-source.mjs")],
        input=json.dumps({"packet": status["packet"], "dispatch": False}).encode("utf8"),
        capture_output=True,
        check=False,
        timeout=10,
    )
    assert result.returncode == 0, result.stdout.decode("utf8")
    assert len(json.loads(result.stdout)["scope_fields"]) == 9
