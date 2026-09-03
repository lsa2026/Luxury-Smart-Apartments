from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("reservations", "0005_customer_and_sandbox_reservation")]

    operations = [
        migrations.AddField(
            model_name="bookingintent",
            name="billing_city",
            field=models.CharField(default="", max_length=80),
        ),
        migrations.AddField(
            model_name="bookingintent",
            name="billing_country",
            field=models.CharField(default="SA", max_length=2),
        ),
        migrations.AddField(
            model_name="bookingintent",
            name="billing_postcode",
            field=models.CharField(default="", max_length=16),
        ),
        migrations.AddField(
            model_name="bookingintent",
            name="billing_state",
            field=models.CharField(default="", max_length=50),
        ),
        migrations.AddField(
            model_name="bookingintent",
            name="billing_street1",
            field=models.CharField(default="", max_length=100),
        ),
    ]
