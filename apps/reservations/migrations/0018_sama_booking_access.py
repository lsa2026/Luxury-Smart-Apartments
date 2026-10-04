from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("reservations", "0017_alter_bookingmodificationrequest_status")]
    operations = [
        migrations.AddField(
            "manualbookingdraft",
            "sama_request_id",
            models.UUIDField(null=True, blank=True, unique=True, editable=False),
        ),
        migrations.AddField(
            "manualbookingdraft",
            "sama_request_fingerprint",
            models.CharField(max_length=64, blank=True, editable=False, db_default=""),
        ),
        migrations.AddField(
            "manualbookingdraft",
            "sama_confirmation_started_at",
            models.DateTimeField(null=True, blank=True, editable=False),
        ),
        migrations.AddField(
            "manualbookingdraft",
            "sama_accounting_result",
            models.CharField(max_length=64, blank=True, editable=False, db_default=""),
        ),
    ]
