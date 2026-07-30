from django.db import migrations, models
from django.db.models import Q


class Migration(migrations.Migration):
    dependencies = [
        ("properties", "0004_property_hostaway_special_status_and_type_rename"),
    ]

    operations = [
        migrations.RenameField(
            model_name="property",
            old_name="hostaway_listing_map_id",
            new_name="hostaway_listing_id",
        ),
        migrations.AddField(
            model_name="property",
            name="hostaway_listing_map_id",
            field=models.PositiveBigIntegerField(
                blank=True,
                db_index=True,
                null=True,
            ),
        ),
        migrations.AddConstraint(
            model_name="property",
            constraint=models.UniqueConstraint(
                condition=Q(hostaway_listing_map_id__isnull=False),
                fields=("hostaway_listing_map_id",),
                name="unique_property_listing_map_id_when_set",
            ),
        ),
    ]
