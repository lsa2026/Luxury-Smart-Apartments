import uuid

from django.db import migrations, models
from django.utils import timezone


class Migration(migrations.Migration):
    dependencies = [("reservations", "0034_stay_extensions")]

    operations = [
        migrations.CreateModel(
            name="WebsiteMessageRelay",
            fields=[
                (
                    "id",
                    models.UUIDField(
                        default=uuid.uuid4, editable=False, primary_key=True, serialize=False
                    ),
                ),
                ("scenario", models.CharField(max_length=2)),
                ("financial", models.BooleanField(default=False)),
                ("event_id", models.UUIDField()),
                ("stay_id", models.UUIDField()),
                ("revision", models.CharField(max_length=32)),
                ("message_type", models.CharField(max_length=32)),
                ("job_sha256", models.CharField(max_length=64)),
                ("source_occurred_at", models.DateTimeField()),
                ("not_before", models.DateTimeField()),
                ("expires_at", models.DateTimeField()),
                ("state", models.CharField(default="pending", max_length=16)),
                ("request_id", models.CharField(blank=True, max_length=128)),
                ("reason", models.CharField(blank=True, max_length=64)),
                ("created_at", models.DateTimeField(default=timezone.now, editable=False)),
                ("updated_at", models.DateTimeField(auto_now=True)),
            ],
            options={
                "indexes": [
                    models.Index(fields=["state", "not_before"], name="website_relay_due_idx")
                ],
                "constraints": [
                    models.UniqueConstraint(
                        fields=("scenario", "financial", "event_id", "message_type"),
                        name="website_relay_event_kind_uniq",
                    )
                ],
            },
        ),
    ]
