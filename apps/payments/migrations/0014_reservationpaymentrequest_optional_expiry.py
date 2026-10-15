from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("payments", "0013_alter_offlinepaymentreceipt_method")]

    operations = [
        migrations.AlterField(
            model_name="reservationpaymentrequest",
            name="expires_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
    ]
