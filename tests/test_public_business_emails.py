import importlib
from types import SimpleNamespace

import pytest
from django.apps import apps
from django.core.cache import cache
from django.db import connection
from django.test import Client, override_settings

from apps.core.guest_documents import VERSION, guest_documents
from apps.core.models import SitePage, SiteSetting
from apps.core.public_contacts import GENERAL_EMAIL, LEGACY_OWNER_EMAIL, RESERVATIONS_EMAIL
from apps.notifications.services.email import EmailMessageRequest, build_guest_email

pytestmark = pytest.mark.django_db


@pytest.mark.parametrize("language", ["ar", "en", "fr"])
@pytest.mark.parametrize("path", ["", "contact/", "about/", "legal/terms/", "legal/privacy/"])
def test_public_pages_publish_both_business_aliases_without_owner_address(language, path):
    # Exercise stale shared-cache values as well as the newly migrated database.
    setting = SiteSetting.objects.get()
    setting.contact_email = LEGACY_OWNER_EMAIL
    cache.set("site:settings", setting, timeout=300)
    try:
        response = Client().get(f"/{language}/{path}")
        assert response.status_code == 200
        html = response.content.decode()
        assert f"mailto:{GENERAL_EMAIL}" in html
        assert f"mailto:{RESERVATIONS_EMAIL}" in html
        assert LEGACY_OWNER_EMAIL not in html
        points = response.context["organization_data"]["contactPoint"]
        assert any(
            p["contactType"] == "reservations" and p["email"] == RESERVATIONS_EMAIL for p in points
        )
    finally:
        cache.delete("site:settings")


def test_alias_migration_changes_only_public_contact_copy_and_is_repeatable():
    setting = SiteSetting.objects.get()
    setting.contact_email = LEGACY_OWNER_EMAIL.upper()
    setting.save()
    page = SitePage.objects.get(slug="contact")
    page.body_en = f"Enquiries: {LEGACY_OWNER_EMAIL}. Keep this custom wording."
    page.body_fr = "Keep the custom French copy."
    page.save()
    migration = importlib.import_module("apps.core.migrations.0026_public_business_emails")
    editor = SimpleNamespace(connection=connection)
    migration.publish_business_emails(apps, editor)
    migration.publish_business_emails(apps, editor)
    setting.refresh_from_db()
    page.refresh_from_db()
    assert setting.contact_email == GENERAL_EMAIL
    assert page.body_en == f"Enquiries: {GENERAL_EMAIL}. Keep this custom wording."
    assert page.body_fr == "Keep the custom French copy."


@pytest.mark.parametrize("language", ["ar", "en", "fr"])
def test_current_guest_document_has_new_contact_revision(language):
    document = guest_documents(language)["terms"]
    assert document["version"] == VERSION == "2026-11-09.1"
    assert GENERAL_EMAIL in document["body"]
    assert LEGACY_OWNER_EMAIL not in document["body"]


@pytest.mark.parametrize(
    ("template", "expected"),
    [
        ("contact", GENERAL_EMAIL),
        ("booking", RESERVATIONS_EMAIL),
        ("reservation", RESERVATIONS_EMAIL),
        ("modification", RESERVATIONS_EMAIL),
    ],
)
@override_settings(
    DEFAULT_FROM_EMAIL="notifications@example.invalid", EMAIL_REPLY_TO=LEGACY_OWNER_EMAIL
)
def test_guest_correspondence_uses_the_contextual_alias_without_changing_sender(template, expected):
    message = build_guest_email(
        EmailMessageRequest(
            recipient="guest@example.invalid",
            subject="Example",
            template_name=template,
            language="en",
            context={"heading": "Example", "message": "Example"},
        )
    )
    assert message.from_email == "notifications@example.invalid"
    assert message.reply_to == [expected]
    assert f"mailto:{expected}" in message.alternatives[0].content
    assert LEGACY_OWNER_EMAIL not in message.body
    assert LEGACY_OWNER_EMAIL not in message.alternatives[0].content
