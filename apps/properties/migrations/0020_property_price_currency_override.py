from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("properties", "0019_property_trustindex_review_metrics"),
    ]

    operations = [
        migrations.AddField(
            model_name="property",
            name="price_currency_override",
            field=models.CharField(
                blank=True,
                help_text=(
                    "Documented property-level correction for a Hostaway currency-labeling "
                    "error. It preserves the price amount but changes its currency interpretation."
                ),
                max_length=3,
            ),
        ),
    ]
