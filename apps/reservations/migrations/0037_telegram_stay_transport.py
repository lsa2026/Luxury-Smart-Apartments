import uuid

from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion
import django.utils.timezone


class Migration(migrations.Migration):
    dependencies = [
        ("reservations", "0036_telegramactivation"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]
    operations = [
        migrations.CreateModel(
            name="TelegramStayChannel",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True, primary_key=True, serialize=False, verbose_name="ID"
                    ),
                ),
                ("scenario", models.CharField(max_length=2)),
                ("stay_id", models.UUIDField()),
                ("hostaway_id", models.CharField(max_length=16)),
                ("cutover_at", models.DateTimeField()),
                (
                    "selected_at",
                    models.DateTimeField(default=django.utils.timezone.now, editable=False),
                ),
                ("receipt_cursor", models.UUIDField(null=True)),
            ],
            options={
                "constraints": [
                    models.UniqueConstraint(
                        fields=("scenario", "stay_id"), name="tg_stay_channel_uniq"
                    )
                ]
            },
        ),
        migrations.CreateModel(
            name="TelegramStayNotice",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True, primary_key=True, serialize=False, verbose_name="ID"
                    ),
                ),
                ("scenario", models.CharField(max_length=2)),
                ("financial", models.BooleanField(default=False)),
                ("event_id", models.UUIDField()),
                ("stay_id", models.UUIDField()),
                ("revision", models.PositiveBigIntegerField()),
                ("state", models.CharField(default="pending", max_length=16)),
                ("attempts", models.PositiveIntegerField(default=0)),
                ("next_attempt_at", models.DateTimeField(default=django.utils.timezone.now)),
                ("lease_until", models.DateTimeField(null=True)),
                ("last_error", models.CharField(blank=True, max_length=64)),
                (
                    "created_at",
                    models.DateTimeField(default=django.utils.timezone.now, editable=False),
                ),
            ],
            options={
                "constraints": [
                    models.UniqueConstraint(
                        fields=("scenario", "financial", "event_id"), name="tg_stay_notice_uniq"
                    )
                ]
            },
        ),
        migrations.CreateModel(
            name="TelegramStayMessage",
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
                ("recipient_kind", models.CharField(default="guest", max_length=8)),
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
            ],
            options={
                "indexes": [
                    models.Index(
                        fields=["scenario", "stay_id", "state"], name="tg_stay_message_state_idx"
                    )
                ]
            },
        ),
        migrations.CreateModel(
            name="TelegramStayActivation",
            fields=[
                (
                    "id",
                    models.UUIDField(
                        default=uuid.uuid4, editable=False, primary_key=True, serialize=False
                    ),
                ),
                ("scenario", models.CharField(max_length=2)),
                ("stay_id", models.UUIDField()),
                ("state", models.CharField(default="processing", max_length=16)),
                ("evidence_ref", models.CharField(max_length=100)),
                ("phone_digest", models.CharField(max_length=64)),
                ("proof_id", models.UUIDField(default=uuid.uuid4, editable=False)),
                ("approve_authorized_at", models.DateTimeField(null=True)),
                ("issue_authorized_at", models.DateTimeField(null=True)),
                ("expires_at", models.DateTimeField(null=True)),
                (
                    "created_at",
                    models.DateTimeField(default=django.utils.timezone.now, editable=False),
                ),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "actor",
                    models.ForeignKey(
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={
                "constraints": [
                    models.UniqueConstraint(
                        fields=("scenario", "stay_id"), name="tg_stay_activation_uniq"
                    )
                ]
            },
        ),
    ]
