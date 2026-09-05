import django.core.validators
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("properties", "0015_indicative_nightly_rate"),
    ]

    operations = [
        migrations.AddField(
            model_name="property",
            name="cancellation_policy_ar",
            field=models.TextField(blank=True, verbose_name="Cancellation policy"),
        ),
        migrations.AddField(
            model_name="property",
            name="cancellation_policy_en",
            field=models.TextField(blank=True, verbose_name="Cancellation policy"),
        ),
        migrations.AddField(
            model_name="property",
            name="cancellation_policy_fr",
            field=models.TextField(blank=True, verbose_name="Cancellation policy"),
        ),
        migrations.AddField(
            model_name="property",
            name="display_check_in_hour",
            field=models.PositiveSmallIntegerField(
                blank=True,
                help_text="Overrides the channel-manager time shown to guests.",
                null=True,
                validators=[
                    django.core.validators.MinValueValidator(0),
                    django.core.validators.MaxValueValidator(24),
                ],
                verbose_name="Guest-facing check-in hour",
            ),
        ),
        migrations.AddField(
            model_name="property",
            name="display_check_out_hour",
            field=models.PositiveSmallIntegerField(
                blank=True,
                help_text="Overrides the channel-manager time shown to guests.",
                null=True,
                validators=[
                    django.core.validators.MinValueValidator(0),
                    django.core.validators.MaxValueValidator(24),
                ],
                verbose_name="Guest-facing check-out hour",
            ),
        ),
    ]
