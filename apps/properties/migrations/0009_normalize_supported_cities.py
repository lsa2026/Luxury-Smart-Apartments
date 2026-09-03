from django.db import migrations
from django.db.models import Q


def normalize_supported_cities(apps, schema_editor):
    Property = apps.get_model("properties", "Property")

    riyadh_aliases = Q(city__iexact="Riyadh") | Q(city__iexact="Riyad")
    riyadh_aliases |= Q(city__iexact="Manea Al Mreidi")
    riyadh_aliases |= Q(city_ar__iexact="الرياض") | Q(city_ar__iexact="عرقة، الرياض")
    riyadh_aliases |= Q(hostaway_listing_id=315814)
    Property.objects.filter(riyadh_aliases).update(
        city="Riyadh",
        city_ar="الرياض",
        city_en="Riyadh",
        city_fr="Riyad",
    )

    marrakech_aliases = Q(city__iexact="Marrakesh") | Q(city__iexact="Marrakech")
    marrakech_aliases |= Q(city_ar__iexact="مراكش")
    Property.objects.filter(marrakech_aliases).update(
        city="Marrakesh",
        city_ar="مراكش",
        city_en="Marrakech",
        city_fr="Marrakech",
    )


class Migration(migrations.Migration):
    dependencies = [
        ("properties", "0008_seed_current_property_translations"),
    ]

    operations = [
        migrations.RunPython(normalize_supported_cities, migrations.RunPython.noop),
    ]
