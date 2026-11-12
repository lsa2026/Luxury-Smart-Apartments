from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("payments", "0020_dayusepaymentattempt_stay_and_more")]

    operations = [
        migrations.AlterField(
            model_name="hostawayfinancialentry",
            name="remote_charge_id",
            field=models.CharField(max_length=64, null=True, blank=True),
        ),
    ]
