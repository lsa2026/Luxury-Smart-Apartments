"""Separate guest agreements, multilingual copy, snapshots and no backfill."""

import hashlib
import re
from decimal import Decimal

import pytest
from django.conf import settings
from django.core.cache import cache
from django.test import Client
from django.utils import translation

from apps.core.guest_documents import VERSION, documents_digest, guest_documents
from apps.reservations.booking_forms import GuestDetailsForm
from apps.reservations.models import BookingIntent
from apps.reservations.views import GuestDetailsView
from tests.test_booking_models_services import guest_data, make_availability
from tests.test_booking_views_admin import RevalidationService, form_data, owned_client_quote

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def isolated_rate_limits():
    cache.clear()
    yield
    cache.clear()


@pytest.mark.parametrize(
    "language,pet_rule,title",
    [
        ("ar", "يُمنع اصطحاب الحيوانات الأليفة", "تعليمات المنزل"),
        ("en", "Pets are prohibited", "House rules"),
        ("fr", "Les animaux de compagnie sont interdits", "Règlement intérieur"),
    ],
)
def test_documents_have_native_copy_and_complete_numbered_terms(language, pet_rule, title):
    documents = guest_documents(language)
    assert documents["house_rules"]["title"] == title
    assert len(documents["house_rules"]["items"]) == 8
    assert pet_rule in documents["house_rules"]["body"]
    assert pet_rule in documents["terms"]["body"]
    assert len(re.findall(r"^## ", documents["terms"]["body"], re.M)) == 14
    for document in documents.values():
        assert document["version"] == VERSION
        assert document["language"] == language
        assert document["sha256"] == hashlib.sha256(document["body"].encode()).hexdigest()
        assert "[" not in document["body"]  # No draft placeholders published.


@pytest.mark.parametrize("language", ["ar", "en", "fr"])
@pytest.mark.parametrize("slug", ["terms", "house-rules", "privacy"])
def test_legal_pages_have_independent_language_urls_and_correct_direction(language, slug):
    response = Client().get(f"/{language}/legal/{slug}/")
    assert response.status_code == 200
    content = response.content.decode()
    assert f'lang="{language}"' in content
    assert f'dir="{"rtl" if language == "ar" else "ltr"}"' in content
    assert VERSION in content
    if slug == "terms":
        assert "1010792573" in content and "7028510308" in content
    if slug == "privacy":
        assert "HyperPay" in content and "Hostaway" in content
        assert "<h2>1." not in content
    if slug == "house-rules":
        assert "<ol" in content
        assert guest_documents(language)["house_rules"]["items"][2] in content


@pytest.mark.parametrize("language", ["ar", "en", "fr"])
def test_quote_has_two_unchecked_required_agreements_and_readable_documents(language):
    client, _, reference = owned_client_quote()
    client.cookies[settings.LANGUAGE_COOKIE_NAME] = language
    response = client.get(f"/reservations/quotes/{reference}/")
    content = response.content.decode()
    for name in ("terms_accepted", "house_rules_accepted"):
        checkbox = re.search(rf'<input[^>]+name="{name}"[^>]*>', content)[0]
        assert "required" in checkbox
        assert "checked" not in checkbox
    assert 'name="privacy_accepted"' not in content
    assert f"/{language}/legal/house-rules/" in content
    assert f"/{language}/legal/terms/" in content
    assert 'name="documents_digest"' in content
    assert guest_documents(language)["house_rules"]["items"][2] in content


@pytest.mark.parametrize("missing", ["terms_accepted", "house_rules_accepted", "documents_digest"])
def test_missing_agreement_cannot_create_an_intent_or_call_a_provider(monkeypatch, missing):
    client, _, reference = owned_client_quote()
    submitted = form_data()
    submitted.pop(missing)
    RevalidationService.calls = 0
    monkeypatch.setattr(GuestDetailsView, "service_class", RevalidationService)
    response = client.post(f"/reservations/quotes/{reference}/guest-details/", submitted)
    assert response.status_code == 400
    assert BookingIntent.objects.count() == 0
    assert RevalidationService.calls == 0
    assert 'data-initial-step="2"' in response.content.decode()


def test_old_document_digest_requires_fresh_acceptance():
    form = GuestDetailsForm({**form_data(), "documents_digest": "0" * 64})
    assert not form.is_valid()
    assert "documents_digest" in form.errors
    assert form.initial["documents_digest"] == documents_digest()


@pytest.mark.parametrize("language", ["ar", "en", "fr"])
def test_success_stores_both_original_documents_and_price_without_new_payment(
    monkeypatch, language
):
    client, quote, reference = owned_client_quote()
    client.cookies[settings.LANGUAGE_COOKIE_NAME] = language
    RevalidationService.result = make_availability(quote.property)
    RevalidationService.calls = 0
    monkeypatch.setattr(GuestDetailsView, "service_class", RevalidationService)
    response = client.post(f"/reservations/quotes/{reference}/guest-details/", form_data())
    assert response.status_code == 302
    intent = BookingIntent.objects.get()
    assert intent.house_rules_accepted_at == intent.terms_accepted_at
    assert intent.legal_acceptance["consents"] == {"terms": True, "house_rules": True}
    assert intent.legal_acceptance["documents"] == guest_documents(language)
    assert intent.legal_acceptance["rate_conditions"]["currency"] == quote.currency
    assert Decimal(intent.legal_acceptance["rate_conditions"]["total"]) == quote.total_price
    assert intent.payment_attempts.count() == 0
    first = dict(intent.legal_acceptance)
    repeated = client.post(f"/reservations/quotes/{reference}/guest-details/", form_data())
    assert repeated.status_code == 302
    intent.refresh_from_db()
    assert intent.legal_acceptance == first
    assert BookingIntent.objects.count() == 1
    saved = client.get(response.url).content.decode()
    assert guest_documents(language)["house_rules"]["title"] in saved


def test_internal_or_historical_intents_are_not_falsely_marked_as_new_guest_consent():
    from apps.reservations.services.booking import consume_revalidated_quote

    _, quote, _ = owned_client_quote()
    outcome = consume_revalidated_quote(
        quote_id=quote.pk,
        session_hash=quote.session_key_hash,
        idempotency_key="z" * 32,
        guest_data=guest_data(),
        revalidated=make_availability(quote.property),
    )
    assert outcome.intent.house_rules_accepted_at is None
    assert outcome.intent.legal_acceptance == {}


@pytest.mark.parametrize("country", ["SA", "MA"])
def test_legacy_property_text_cannot_reintroduce_permission_for_pets(country):
    from apps.reservations.services.stay_policy import stay_policy_for
    from tests.test_stay_policy_display import make_property

    apartment = make_property(country_code=country, house_rules_en="Pets are welcome.")
    with translation.override("en"):
        rules = stay_policy_for(apartment).house_rules
        assert "Pets are welcome" not in rules
        assert "Pets are prohibited" in rules
    apartment.refresh_from_db()
    assert apartment.house_rules_en == "Pets are welcome."  # Source preserved, not erased.


def test_accepted_copy_stays_available_after_secure_booking_recovery():
    from apps.reservations.access_tokens import make_access_link_token
    from tests.test_account_booking_claim import make_reservation

    reservation = make_reservation("LSA-DOCUMENT-COPY")
    intent = reservation.booking_intent
    documents = guest_documents("fr")
    # Saved historic evidence must win over today's public copy.
    documents["terms"]["body"] = "Accepted historical conditions."
    intent.language = "fr"
    intent.legal_acceptance = {"documents": documents, "accepted_at": "2026-10-05T05:00:00Z"}
    intent.save(update_fields=["language", "legal_acceptance"])
    client = Client()
    manage_url = f"/reservations/manage/{reservation.public_reference}/"
    assert client.get(manage_url).status_code == 404
    token = make_access_link_token(reservation.public_reference, "guest@example.invalid")
    assert client.get(f"/reservations/manage/access/{token}/").status_code == 302
    response = client.get(manage_url)
    assert response.status_code == 200
    content = response.content.decode()
    assert "Accepted historical conditions." in content
    assert 'lang="fr"' in content
    assert "no-store" in response["Cache-Control"]
