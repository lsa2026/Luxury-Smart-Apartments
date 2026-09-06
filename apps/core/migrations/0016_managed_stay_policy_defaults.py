from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0015_simplify_arabic_tagline"),
    ]

    operations = [
        migrations.AddField(
            model_name="sitesetting",
            name="default_cancellation_policy_ar",
            field=models.TextField(blank=True, verbose_name="Default cancellation policy"),
        ),
        migrations.AddField(
            model_name="sitesetting",
            name="default_cancellation_policy_en",
            field=models.TextField(blank=True, verbose_name="Default cancellation policy"),
        ),
        migrations.AddField(
            model_name="sitesetting",
            name="default_cancellation_policy_fr",
            field=models.TextField(blank=True, verbose_name="Default cancellation policy"),
        ),
    ]
