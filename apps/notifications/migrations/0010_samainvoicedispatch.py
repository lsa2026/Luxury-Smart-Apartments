import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("notifications", "0009_guestinvoicepresentation_and_more")]

    operations = [
        migrations.CreateModel(
            name="SamaInvoiceDispatch",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True, primary_key=True, serialize=False, verbose_name="ID"
                    ),
                ),
                ("request_id", models.UUIDField(unique=True)),
                ("pdf_sha256", models.CharField(max_length=64)),
                ("pdf_bytes", models.PositiveIntegerField()),
                ("pdf_filename", models.CharField(max_length=150)),
                ("recipient_sha256", models.CharField(max_length=64)),
                ("language", models.CharField(max_length=2)),
                ("caption", models.TextField()),
                ("caption_sha256", models.CharField(max_length=64)),
                ("source_fingerprint", models.CharField(max_length=64)),
                ("not_before", models.DateTimeField()),
                ("expires_at", models.DateTimeField()),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("outbox_id", models.UUIDField(blank=True, null=True, unique=True)),
                ("provider_state", models.CharField(blank=True, max_length=20)),
                ("provider_message_id", models.CharField(blank=True, max_length=255)),
                ("wamid", models.CharField(blank=True, max_length=255)),
                ("business_sender", models.CharField(blank=True, max_length=32)),
                ("waba_id", models.CharField(blank=True, max_length=32)),
                ("phone_id", models.CharField(blank=True, max_length=32)),
                ("receipt_evidence_ref", models.CharField(blank=True, max_length=255)),
                ("observed_at", models.DateTimeField(blank=True, null=True)),
                ("accepted_at", models.DateTimeField(blank=True, null=True)),
                ("proof_sha256", models.CharField(blank=True, max_length=64)),
                (
                    "delivery",
                    models.OneToOneField(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="sma_dispatch",
                        to="notifications.guestinvoicedelivery",
                    ),
                ),
            ],
        ),
    ]
