from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ("properties", "0001_initial"),
    ]

    operations = [
        migrations.RenameField(
            model_name="property",
            old_name="hostaway_listing_id",
            new_name="hostaway_listing_map_id",
        ),
    ]
