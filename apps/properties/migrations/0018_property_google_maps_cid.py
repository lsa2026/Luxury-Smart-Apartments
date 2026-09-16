from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("properties", "0017_managed_stay_policy"),
    ]

    operations = [
        migrations.AddField(
            model_name="property",
            name="google_maps_cid",
            field=models.CharField(blank=True, max_length=32, verbose_name="Google Maps business identifier"),
        ),
    ]
