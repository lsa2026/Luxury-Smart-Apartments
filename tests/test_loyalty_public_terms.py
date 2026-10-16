"""Issued public terms are readable without an account and consent is version-bound."""

import hashlib
import json
from datetime import UTC, datetime
from html import unescape

import pytest
from django.core.exceptions import ValidationError
from django.test import Client
from django.utils import timezone, translation

from apps.accounts.models import PhoneChallenge, VerifiedPhone
from apps.loyalty.models import Ledger, Member
from apps.loyalty.public_terms import (
    CONTENT,
    OPERATOR,
    REFERENCE,
    URL_VERSION,
    VERSION,
    consent_metadata,
    document,
    terms_reference,
)
from tests.test_loyalty_stage8 import user

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def public_release(settings, monkeypatch):
    settings.LOYALTY_ENABLED = settings.LOYALTY_UI_ENABLED = True
    settings.LOYALTY_PILOT_ONLY = False
    settings.LOYALTY_PILOT_EMAILS = []
    settings.LOYALTY_UI_PREVIEW = False
    settings.LOYALTY_LAUNCH_AT = "2026-10-01T00:00:00+03:00"
    settings.LOYALTY_TERMS_APPROVED = True
    settings.LOYALTY_TERMS_VERSION = VERSION
    settings.LOYALTY_TERMS_REFERENCE = REFERENCE
    monkeypatch.setattr(timezone, "now", lambda: datetime(2026, 10, 16, 13, tzinfo=UTC))


@pytest.mark.parametrize("language", ["ar", "en", "fr"])
def test_public_terms_available_to_anonymous_guests_in_each_language(language, settings):
    settings.LOYALTY_ENABLED = settings.LOYALTY_UI_ENABLED = False
    settings.LOYALTY_TERMS_APPROVED = False
    client = Client()
    url = f"/{language}/legal/rewards/{URL_VERSION}/"
    response = client.get(url)
    assert response.status_code == 200
    content = unescape(response.content.decode())
    issued = document(language)
    assert issued["title"] in content and issued["operator"] in content
    assert "rewards-terms-page" in content and "css/rewards-terms.css" in content
    assert len(issued["sections"]) == 14
    assert response["X-Document-Version"] == VERSION
    assert response["X-Document-SHA256"] == issued["sha256"]
    assert (
        issued["sha256"]
        == hashlib.sha256(
            json.dumps(CONTENT[language], ensure_ascii=False, sort_keys=True).encode()
        ).hexdigest()
    )
    assert f'href="/{language}/legal/privacy/"' in content
    assert f'href="/{language}/contact/"' in content
    assert client.head(url).status_code == 200
    assert client.post(url, {"consent": "on"}).status_code == 405
    assert client.get(f"/{language}/legal/rewards/unknown-version/").status_code == 404
    assert not Member.objects.exists() and not Ledger.objects.exists()
    assert not VerifiedPhone.objects.exists() and not PhoneChallenge.objects.exists()


@pytest.mark.parametrize("language", ["ar", "en", "fr"])
def test_public_enrolment_links_and_records_actual_localized_terms(language, settings):
    guest = user("published-" + language)
    client = Client()
    client.force_login(guest)
    client.cookies[settings.LANGUAGE_COOKIE_NAME] = language
    url = f"/{language}/legal/rewards/{URL_VERSION}/"
    page = client.get("/my-bookings/rewards/")
    assert page.status_code == 200 and page.context["loyalty_terms_reference"] == url
    assert f'href="{url}"' in page.content.decode()
    assert client.post("/my-bookings/rewards/", {"action": "join"}).status_code == 200
    assert not Member.objects.exists()
    response = client.post(
        "/my-bookings/rewards/",
        {"action": "join", "consent": "on", "terms_version": "forged", "terms_reference": "bad"},
    )
    assert response.status_code == 302
    member = Member.objects.get(customer=guest)
    assert member.terms_version == VERSION and member.terms_reference == url
    assert member.consent_history[0]["language"] == language
    assert member.consent_history[0]["terms_content_sha256"] == document(language)["sha256"]
    assert not Ledger.objects.exists()
    assert f'href="{url}"' in client.get("/my-bookings/rewards/").content.decode()


def test_fictional_or_preexisting_terms_references_are_not_rewritten(settings):
    settings.LOYALTY_TERMS_REFERENCE = "/fictional-terms/"
    with translation.override("fr"):
        assert terms_reference() == "/fictional-terms/"
    assert consent_metadata("old-version", "old-reference", "ar") == {}
    with pytest.raises(ValidationError):
        consent_metadata(VERSION, REFERENCE, "fr")
    with pytest.raises(ValidationError):
        consent_metadata(VERSION, REFERENCE, "unknown")


def test_only_authorised_business_identity_is_published():
    assert OPERATOR == {
        "name": "مؤسسة الحياة الذكية التجارية",
        "registration": "1010792573",
        "unified_number": "7028510308",
    }
    for text in CONTENT.values():
        assert (
            "المغرب" in text["operator"]
            or "Morocco" in text["operator"]
            or "Maroc" in text["operator"]
        )
        assert "docs.google.com" not in json.dumps(text)
