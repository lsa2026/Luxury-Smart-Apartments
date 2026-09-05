from django.db import migrations


def simplify_arabic_tagline(apps, schema_editor):
    del schema_editor
    site_setting = apps.get_model("core", "SiteSetting")
    site_setting.objects.filter(
        tagline_ar__in=(
            "إقامات ذكية فاخرة",
            "إقامات ذكية فاخرة ",
            "إقامات ذكية فاخرة في الرياض",
            "إقامات ذكية فاخرة ومراكش",
            "إقامات ذكية فاخرة  ومراكش.",
        )
    ).update(tagline_ar="إقامة ذكية فاخرة")


class Migration(migrations.Migration):
    dependencies = [("core", "0014_merge_20260905_0300")]

    operations = [migrations.RunPython(simplify_arabic_tagline, migrations.RunPython.noop)]
