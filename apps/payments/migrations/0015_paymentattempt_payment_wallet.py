from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("payments", "0014_reservationpaymentrequest_optional_expiry")]

    operations = [
        migrations.AddField(
            model_name="paymentattempt",
            name="payment_wallet",
            field=models.CharField(blank=True, editable=False, max_length=20),
        ),
    ]
