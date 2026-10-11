from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("reservations", "0021_reservation_loyalty_state")]
    operations = [
        migrations.AddField(
            model_name="bookingquote",
            name="night_prices",
            field=models.JSONField(blank=True, default=dict, editable=False),
        )
    ]
