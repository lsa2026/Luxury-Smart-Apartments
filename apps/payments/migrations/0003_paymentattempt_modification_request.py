from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        ("payments", "0002_initial"),
        ("reservations", "0005_customer_and_sandbox_reservation"),
    ]

    operations = [
        migrations.AddField(
            model_name="paymentattempt",
            name="modification_request",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="payment_attempts",
                to="reservations.bookingmodificationrequest",
            ),
        ),
    ]
