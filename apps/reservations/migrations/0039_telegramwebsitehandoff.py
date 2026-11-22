import uuid

import django.utils.timezone
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("reservations", "0038_telegramworkgrouphandoff")]
    operations = [
        migrations.CreateModel(
            name="TelegramWebsiteHandoff",
            fields=[
                (
                    "id",
                    models.UUIDField(
                        default=uuid.uuid4, editable=False, primary_key=True, serialize=False
                    ),
                ),
                ("source_kind", models.CharField(max_length=32)),
                ("source_id", models.CharField(max_length=180)),
                ("source_revision", models.CharField(max_length=64)),
                ("job_key", models.CharField(max_length=220, unique=True)),
                ("reference_id", models.CharField(max_length=100, unique=True)),
                ("phone_hash", models.CharField(max_length=64)),
                ("content_sha256", models.CharField(max_length=64)),
                ("asset_sha256", models.CharField(max_length=64)),
                ("state", models.CharField(default="planned", max_length=16)),
                ("attempt_id", models.UUIDField(null=True)),
                ("provider_message_id", models.CharField(blank=True, max_length=32)),
                ("provider_message_ids", models.JSONField(default=list)),
                ("accepted_at", models.DateTimeField(null=True)),
                ("not_before", models.DateTimeField()),
                ("expires_at", models.DateTimeField()),
                ("probes", models.PositiveSmallIntegerField(default=0)),
                ("next_probe_at", models.DateTimeField(default=django.utils.timezone.now)),
                (
                    "created_at",
                    models.DateTimeField(default=django.utils.timezone.now, editable=False),
                ),
                ("updated_at", models.DateTimeField(auto_now=True)),
            ],
        )
    ]
