from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("reservations", "0045_websitemessagerelay_payment_request_delivery_and_more")]

    operations = [
        migrations.AddField(
            model_name="manualbookingdraft",
            name="extension_contact_provenance",
            field=models.JSONField(blank=True, db_default={}, default=dict, editable=False),
        ),
    ]
