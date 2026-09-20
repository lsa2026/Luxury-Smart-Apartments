from django.db import migrations
from django.db.models import Q


def normalize_marrakech_spelling(apps, schema_editor) -> None:
    del schema_editor
    property_model = apps.get_model("properties", "Property")
    legacy_city = Q(city__iexact="Marrakesh") | Q(city__iexact="Marrakech")
    property_model.objects.filter(legacy_city).update(
        city="Marrakech",
        city_en="Marrakech",
    )
    property_model.objects.filter(city_en__iexact="Marrakesh").update(city_en="Marrakech")


class Migration(migrations.Migration):
    dependencies = [
        ("properties", "0020_property_price_currency_override"),
    ]

    operations = [
        migrations.RunPython(normalize_marrakech_spelling, migrations.RunPython.noop),
    ]
