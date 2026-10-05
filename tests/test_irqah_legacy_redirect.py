"""Irqah's old URL has one permanent Arabic destination; no provider writes."""

import importlib
from types import SimpleNamespace

import pytest
from django.apps import apps
from django.conf import settings
from django.test import Client

from apps.core.models import LegacyRedirect
from apps.properties.models import Property

pytestmark = pytest.mark.django_db

migration = importlib.import_module("apps.core.migrations.0021_irqah_legacy_listing_redirect")


@pytest.mark.parametrize("language", ["ar", "en", "fr"])
@pytest.mark.parametrize("method", ["get", "head"])
def test_old_listing_always_returns_one_hop_permanent_arabic_redirect(language, method):
    client = Client()
    client.cookies[settings.LANGUAGE_COOKIE_NAME] = language
    response = getattr(client, method)(
        migration.SOURCE_PATH,
        HTTP_ACCEPT_LANGUAGE=language,
    )
    assert response.status_code == 301
    assert response["Location"] == migration.DESTINATION_PATH


@pytest.mark.parametrize(
    "query", ["?ved=1t:202767&ictx=111", "?language=fr", "?next=https://example.invalid"]
)
def test_search_parameters_cannot_change_the_fixed_redirect_destination(query):
    response = Client().get(migration.SOURCE_PATH + query)
    assert response.status_code == 301
    assert response["Location"] == migration.DESTINATION_PATH


def test_redirect_lands_on_existing_arabic_property_without_changing_its_slug():
    apartment = Property.objects.create(
        hostaway_listing_id=889814,
        slug="spacious-and-modern-apartment-for-rent-in-riyadh",
        name_ar="شقة عرقة الذكية",
        name_en="Irqah smart apartment",
        is_visible=True,
    )
    client = Client()
    client.cookies[settings.LANGUAGE_COOKIE_NAME] = "fr"
    redirect = client.get(migration.SOURCE_PATH)
    assert redirect.status_code == 301
    assert redirect["Location"] == migration.DESTINATION_PATH
    response = client.get(redirect["Location"])
    assert response.status_code == 200
    assert 'lang="ar"' in response.content.decode()
    assert apartment.name_ar in response.content.decode()
    apartment.refresh_from_db()
    assert apartment.slug == "spacious-and-modern-apartment-for-rent-in-riyadh"


def test_data_migration_is_idempotent_and_preserves_other_redirects_and_hit_history():
    redirect = LegacyRedirect.objects.get(source_path=migration.SOURCE_PATH)
    redirect.hit_count = 9
    redirect.save(update_fields=["hit_count"])
    others = list(
        LegacyRedirect.objects.exclude(pk=redirect.pk).values(
            "id", "source_path", "destination_path", "redirect_type", "is_active"
        )
    )
    schema_editor = SimpleNamespace(connection=SimpleNamespace(alias="default"))
    migration.add_irqah_redirect(apps, schema_editor)
    migration.add_irqah_redirect(apps, schema_editor)
    assert LegacyRedirect.objects.filter(source_path=migration.SOURCE_PATH).count() == 1
    redirect.refresh_from_db()
    assert redirect.hit_count == 9
    assert redirect.destination_path == migration.DESTINATION_PATH
    assert redirect.redirect_type == 301
    assert redirect.is_active
    assert others == list(
        LegacyRedirect.objects.exclude(pk=redirect.pk).values(
            "id", "source_path", "destination_path", "redirect_type", "is_active"
        )
    )


def test_unrelated_missing_listing_is_not_redirected_to_irqah():
    response = Client().get("/listing/unknown-synthetic-apartment/")
    assert response.status_code == 404
    assert "Location" not in response
