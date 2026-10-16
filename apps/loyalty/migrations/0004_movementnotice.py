import uuid

import django.db.models.deletion
import django.utils.timezone
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("loyalty", "0003_referralrevision"),
        ("accounts", "0005_phoneauthlimit_phonechallenge_verifiedphone"),
    ]
    operations = [
        migrations.CreateModel(
            name="MovementNotice",
            fields=[
                (
                    "id",
                    models.UUIDField(
                        primary_key=True, default=uuid.uuid4, editable=False, serialize=False
                    ),
                ),
                ("binding_version", models.UUIDField(null=True, editable=False)),
                ("phone_digest", models.CharField(max_length=64, blank=True, editable=False)),
                ("language", models.CharField(max_length=2)),
                (
                    "template_version",
                    models.CharField(max_length=40, default="movement-whatsapp-v1"),
                ),
                (
                    "status",
                    models.CharField(
                        max_length=12,
                        default="queued",
                        choices=[
                            (v, v)
                            for v in (
                                "queued",
                                "sending",
                                "accepted",
                                "uncertain",
                                "failed",
                                "skipped",
                            )
                        ],
                    ),
                ),
                ("attempts", models.PositiveSmallIntegerField(default=0)),
                (
                    "next_attempt_at",
                    models.DateTimeField(default=django.utils.timezone.now, db_index=True),
                ),
                ("started_at", models.DateTimeField(null=True)),
                ("accepted_at", models.DateTimeField(null=True)),
                ("error_code", models.CharField(max_length=40, blank=True)),
                ("provider_reference", models.CharField(max_length=80, blank=True)),
                ("created_at", models.DateTimeField(default=django.utils.timezone.now)),
                (
                    "ledger",
                    models.OneToOneField(
                        to="loyalty.ledger", on_delete=django.db.models.deletion.PROTECT
                    ),
                ),
                (
                    "recipient_binding",
                    models.ForeignKey(
                        to="accounts.verifiedphone",
                        null=True,
                        editable=False,
                        on_delete=django.db.models.deletion.SET_NULL,
                    ),
                ),
            ],
            options={
                "indexes": [
                    models.Index(fields=["status", "next_attempt_at"], name="loyalty_notice_due")
                ]
            },
        ),
    ]
