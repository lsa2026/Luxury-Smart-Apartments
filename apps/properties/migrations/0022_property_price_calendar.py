import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("properties", "0021_normalize_marrakech_spelling")]

    operations = [
        migrations.CreateModel(
            name="PropertyPriceCalendar",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True, primary_key=True, serialize=False, verbose_name="ID"
                    ),
                ),
                ("currency", models.CharField(max_length=3)),
                ("start_date", models.DateField()),
                ("end_date", models.DateField()),
                ("days", models.JSONField(default=list)),
                ("fetched_at", models.DateTimeField()),
                (
                    "property",
                    models.OneToOneField(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="price_calendar",
                        to="properties.property",
                    ),
                ),
            ],
        ),
    ]
