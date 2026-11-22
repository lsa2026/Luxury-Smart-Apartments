import uuid

import django.utils.timezone
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("reservations", "0037_telegram_stay_transport")]

    operations = [
        migrations.CreateModel(
            name="TelegramWorkgroupHandoff",
            fields=[
                (
                    "id",
                    models.UUIDField(
                        default=uuid.uuid4, editable=False, primary_key=True, serialize=False
                    ),
                ),
                ("scenario", models.CharField(max_length=2)),
                ("stay_id", models.UUIDField()),
                ("event_id", models.UUIDField()),
                ("revision", models.PositiveBigIntegerField()),
                ("purpose", models.CharField(max_length=32)),
                ("sequence", models.PositiveSmallIntegerField()),
                ("job_key", models.CharField(max_length=220, unique=True)),
                ("reference_id", models.CharField(max_length=220, unique=True)),
                ("identity", models.JSONField()),
                ("booking_basis", models.JSONField()),
                ("state", models.CharField(default="planned", max_length=16)),
                ("content_sha256", models.CharField(blank=True, max_length=64)),
                ("asset_ref", models.CharField(blank=True, max_length=100)),
                ("asset_sha256", models.CharField(blank=True, max_length=64)),
                ("render_sha256", models.CharField(blank=True, max_length=64)),
                ("attempt_id", models.UUIDField(null=True)),
                ("provider_message_id", models.CharField(blank=True, max_length=32)),
                ("binding_fingerprint", models.CharField(blank=True, max_length=64)),
                ("accepted_at", models.DateTimeField(null=True)),
                ("not_before", models.DateTimeField()),
                ("expires_at", models.DateTimeField()),
                (
                    "created_at",
                    models.DateTimeField(default=django.utils.timezone.now, editable=False),
                ),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("recipient_kind", models.CharField(default="workgroup", max_length=16)),
            ],
            options={
                "indexes": [
                    models.Index(
                        fields=["scenario", "stay_id", "state"], name="tg_workgroup_state_idx"
                    )
                ]
            },
        )
    ]
