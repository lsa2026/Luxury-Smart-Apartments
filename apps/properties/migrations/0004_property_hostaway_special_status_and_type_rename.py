from django.db import migrations, models


def preserve_existing_activity_state(apps, schema_editor) -> None:
    Property = apps.get_model("properties", "Property")
    Property.objects.filter(hostaway_is_active=False).update(
        hostaway_special_status="archived"
    )


def restore_activity_from_status(apps, schema_editor) -> None:
    Property = apps.get_model("properties", "Property")
    Property.objects.update(hostaway_is_active=True)
    Property.objects.filter(hostaway_special_status__iexact="archived").update(
        hostaway_is_active=False
    )


class Migration(migrations.Migration):
    dependencies = [
        ("properties", "0003_amenity_propertyamenity_propertyimage_and_more"),
    ]

    operations = [
        migrations.RenameField(
            model_name="property",
            old_name="property_type",
            new_name="hostaway_property_type_id",
        ),
        migrations.AddField(
            model_name="property",
            name="hostaway_special_status",
            field=models.CharField(blank=True, default="", max_length=100),
            preserve_default=False,
        ),
        migrations.RunPython(
            preserve_existing_activity_state,
            restore_activity_from_status,
        ),
    ]
