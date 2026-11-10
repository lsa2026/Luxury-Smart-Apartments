import uuid

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("reservations", "0032_manualbookingdraft_billing_address")]

    operations = [
        migrations.CreateModel(
            name="GuestDirectorySnapshot",
            fields=[
                (
                    "id",
                    models.PositiveSmallIntegerField(
                        default=1, editable=False, primary_key=True, serialize=False
                    ),
                ),
                ("data", models.JSONField(default=dict)),
                ("completed_at", models.DateTimeField()),
            ],
        ),
        migrations.CreateModel(
            name="AdminGuestFile",
            fields=[
                (
                    "id",
                    models.UUIDField(
                        default=uuid.uuid4, editable=False, primary_key=True, serialize=False
                    ),
                ),
                ("identity_key", models.CharField(max_length=64, unique=True)),
                ("email_key", models.CharField(blank=True, max_length=64)),
                ("phone_key", models.CharField(blank=True, max_length=64)),
                ("classification", models.CharField(default="unknown", max_length=16)),
                ("basis", models.CharField(blank=True, max_length=250)),
                ("history", models.JSONField(default=list)),
                ("checked_at", models.DateTimeField(null=True)),
            ],
        ),
        migrations.CreateModel(
            name="AdminGuestAnchor",
            fields=[
                (
                    "hostaway_reservation_id",
                    models.PositiveBigIntegerField(primary_key=True, serialize=False),
                ),
                (
                    "guest_file",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="anchors",
                        to="reservations.adminguestfile",
                    ),
                ),
            ],
        ),
    ]
