import pytest
from django.db import connection
from django.db.migrations.executor import MigrationExecutor

pytestmark = pytest.mark.django_db(transaction=True)


def test_listing_identifier_rename_migrations_preserve_data() -> None:
    executor = MigrationExecutor(connection)
    executor.migrate([("properties", "0001_initial")])
    old_apps = executor.loader.project_state([("properties", "0001_initial")]).apps
    old_property = old_apps.get_model("properties", "Property")
    created = old_property.objects.create(
        hostaway_listing_id=778899,
        slug="migration-property",
        name_ar="وحدة",
        name_en="Property",
        city_ar="الرياض",
        city_en="Riyadh",
        country_code="SA",
    )

    executor = MigrationExecutor(connection)
    executor.migrate([("properties", "0005_correct_hostaway_listing_identifiers")])
    new_apps = executor.loader.project_state(
        [("properties", "0005_correct_hostaway_listing_identifiers")]
    ).apps
    new_property = new_apps.get_model("properties", "Property")
    migrated = new_property.objects.get(pk=created.pk)

    assert migrated.hostaway_listing_id == 778899
    assert migrated.hostaway_listing_map_id is None


def test_property_type_rename_and_activity_state_preserve_data() -> None:
    executor = MigrationExecutor(connection)
    executor.migrate([("properties", "0003_amenity_propertyamenity_propertyimage_and_more")])
    old_apps = executor.loader.project_state(
        [("properties", "0003_amenity_propertyamenity_propertyimage_and_more")]
    ).apps
    old_property = old_apps.get_model("properties", "Property")
    created = old_property.objects.create(
        hostaway_listing_map_id=998877,
        property_type=7,
        hostaway_is_active=False,
        slug="property-type-migration",
    )

    executor = MigrationExecutor(connection)
    executor.migrate([("properties", "0005_correct_hostaway_listing_identifiers")])
    new_apps = executor.loader.project_state(
        [("properties", "0005_correct_hostaway_listing_identifiers")]
    ).apps
    new_property = new_apps.get_model("properties", "Property")
    migrated = new_property.objects.get(pk=created.pk)

    assert migrated.hostaway_property_type_id == 7
    assert migrated.hostaway_listing_id == 998877
    assert migrated.hostaway_listing_map_id is None
    assert not hasattr(migrated, "property_type")
    assert migrated.hostaway_special_status == "archived"
    assert migrated.hostaway_is_active is False
