"""Complete the verified Irqah URL move without changing the property."""

from django.db import migrations

SOURCE_PATH = "/listing/spacious-and-modern-apartment-for-rent-in-riyadh/"
DESTINATION_PATH = "/ar/properties/spacious-and-modern-apartment-for-rent-in-riyadh/"


def add_irqah_redirect(apps, schema_editor):
    redirect_model = apps.get_model("core", "LegacyRedirect")
    redirect_model.objects.using(schema_editor.connection.alias).update_or_create(
        source_path=SOURCE_PATH,
        defaults={
            "destination_path": DESTINATION_PATH,
            "redirect_type": 301,
            "is_active": True,
        },
    )


class Migration(migrations.Migration):
    dependencies = [("core", "0020_set_standard_arrival_and_departure_times")]

    # Keep this harmless URL mapping when rolling back code; do not erase
    # redirect history or any mapping subsequently edited by the operator.
    operations = [migrations.RunPython(add_irqah_redirect, migrations.RunPython.noop)]
