from django.db import migrations


BRAND_NAME = "Luxury Smart Apartments"


def normalize_brand_name(apps, schema_editor):
    del schema_editor
    site_setting = apps.get_model("core", "SiteSetting")
    site_setting.objects.update(
        site_name=BRAND_NAME,
        brand_name_ar=BRAND_NAME,
        brand_name_en=BRAND_NAME,
        brand_name_fr=BRAND_NAME,
    )


class Migration(migrations.Migration):
    dependencies = [("core", "0008_public_information_pages")]

    operations = [
        migrations.RunPython(normalize_brand_name, migrations.RunPython.noop),
    ]
