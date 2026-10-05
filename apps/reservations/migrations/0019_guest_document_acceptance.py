from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("reservations", "0018_sama_booking_access")]
    operations = [
        migrations.AddField(
            model_name="bookingintent",
            name="house_rules_accepted_at",
            field=models.DateTimeField(blank=True, editable=False, null=True),
        ),
        migrations.AddField(
            model_name="bookingintent",
            name="legal_acceptance",
            field=models.JSONField(blank=True, default=dict, editable=False),
        ),
    ]
