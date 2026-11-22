"""Guest choice tests use disposable records and never send a message."""
# ruff: noqa: F811

import os
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

import pytest
from django.contrib.auth import get_user_model
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import VerifiedPhone
from apps.reservations import whatsapp_preferences as choices
from apps.reservations.models import BookingWhatsAppChoice
from tests.test_contact_layout import _standalone
from tests.test_message_source_authorization import record as record  # noqa: F401
from tests.test_message_source_authorization import request_source

pytestmark = pytest.mark.django_db


@pytest.fixture
def guest(record, client, settings):  # noqa: F811
    settings.BOOKING_WHATSAPP_CHOICE_ENABLED = True
    user = get_user_model().objects.create_user(username="synthetic-guest")
    VerifiedPhone.objects.create(user=user, phone=record[1].guest_phone)
    client.force_login(user)
    return user


def url(record):
    return reverse("accounts:booking_whatsapp_choice", args=[record[0], record[1].pk])


def post(client, record, *, service="yes", action="save"):
    page = client.get(url(record))
    return client.post(
        url(record),
        {
            "version": choices.VERSION,
            "latest_id": page.context["latest_id"],
            "booking_revision": str(record[1].communication_revision),
            "action": action,
            "service": service,
        },
    )


def test_choice_is_optional_unchecked_and_not_marketing(client, record, guest):
    page = client.get(url(record))
    assert page.status_code == 200
    assert b'name="service" value="yes">' in page.content
    assert not BookingWhatsAppChoice.objects.exists()
    assert choices.consent_facts(record[1], record[0])["service"] is None
    assert post(client, record).status_code == 302
    row = BookingWhatsAppChoice.objects.get()
    assert row.text_snapshot == choices.COPY["en"]["choice"]
    assert row.text_version == choices.VERSION and row.user_id == guest.pk
    facts = request_source(client, record).json()
    assert facts["consent"]["service"] is True and facts["consent"]["marketing"] is None
    assert facts["consent"]["channel"] == "whatsapp"
    assert facts["consent"]["purpose"] == "booking_updates"
    assert facts["dispatch_allowed"] is False
    assert post(client, record, action="withdraw").status_code == 302
    assert BookingWhatsAppChoice.objects.count() == 2
    assert choices.consent_facts(record[1], record[0])["revoked"] is True


def test_unchecked_does_not_opt_in_and_stale_tab_cannot_restore(client, record, guest):
    assert post(client, record, service="").status_code == 302
    assert choices.consent_facts(record[1], record[0])["service"] is False
    old = client.get(url(record)).context["latest_id"]
    post(client, record)
    post(client, record, action="withdraw")
    response = client.post(
        url(record),
        {
            "version": choices.VERSION,
            "latest_id": old,
            "booking_revision": str(record[1].communication_revision),
            "action": "save",
            "service": "yes",
        },
    )
    assert response.status_code == 409
    assert choices.consent_facts(record[1], record[0])["service"] is False


def test_wrong_identity_signed_payment_link_and_admin_contact_are_not_consent(
    client, record, guest
):
    binding = VerifiedPhone.objects.get(user=guest)
    binding.phone = "+442079460018"
    binding.save()
    assert client.get(url(record)).status_code == 404
    assert client.post(url(record), {"service": "yes"}).status_code == 404
    assert not BookingWhatsAppChoice.objects.exists()
    client.logout()
    assert client.get(url(record)).status_code == 302


def test_binding_rotation_and_booking_revision_invalidate_old_grant(client, record, guest):
    post(client, record)
    binding = VerifiedPhone.objects.get(user=guest)
    binding.version = uuid4()
    binding.save()
    assert choices.consent_facts(record[1], record[0])["service"] is None
    post(client, record)
    record[1].communication_revision += 1
    assert choices.consent_facts(record[1], record[0])["service"] is None


def test_disabled_no_schema_query_and_csrf_required(client, record, guest, settings):
    csrf_client = Client(enforce_csrf_checks=True)
    csrf_client.force_login(guest)
    assert csrf_client.post(url(record), {"service": "yes"}).status_code == 403
    settings.BOOKING_WHATSAPP_CHOICE_ENABLED = False
    assert client.get(url(record)).status_code == 404
    assert choices.consent_facts(record[1], record[0])["service"] is None


@pytest.mark.parametrize("language", ["ar", "en", "fr"])
def test_all_languages_show_brand_channel_and_no_promotional_choice(
    client, record, guest, language, tmp_path
):
    model = type(record[1])
    model.objects.filter(pk=record[1].pk).update(guest_language=language)
    result = client.get(url(record))
    assert choices.COPY[language]["choice"] in result.content.decode()
    assert (
        "WhatsApp" in result.content.decode()
        and "Luxury Smart Apartments" in result.content.decode()
    )
    assert 'name="marketing"' not in result.content.decode()
    output = Path(os.environ.get("LSA_CONTACT_VISUAL_DIR", tmp_path))
    output.mkdir(exist_ok=True, parents=True)
    # Static synthetic page only. Remove the disposable CSRF value from artifacts.
    html = _standalone(result.content.decode())
    import re

    html = re.sub(r'(name="csrfmiddlewaretoken" value=")[^"]*', r"\1synthetic-only", html)
    (output / f"whatsapp-choice-{record[0]}-{language}.html").write_text(html, encoding="utf-8")


def test_latest_withdrawal_wins_even_if_wall_clock_moves_back(client, record, guest):
    post(client, record)
    earlier = timezone.now() - timedelta(hours=1)
    with patch("django.utils.timezone.now", return_value=earlier):
        post(client, record, action="withdraw")
    assert choices.consent_facts(record[1], record[0])["revoked"] is True
    assert list(
        BookingWhatsAppChoice.objects.order_by("sequence").values_list("sequence", flat=True)
    ) == [1, 2]
