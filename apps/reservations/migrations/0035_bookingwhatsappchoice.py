import uuid

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("reservations", "0034_stay_extensions"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]
    operations = [
        migrations.CreateModel(
            name="BookingWhatsAppChoice",
            fields=[
                (
                    "id",
                    models.UUIDField(
                        default=uuid.uuid4, primary_key=True, editable=False, serialize=False
                    ),
                ),
                ("phone_digest", models.CharField(max_length=64, editable=False)),
                ("binding_version", models.UUIDField(editable=False)),
                ("booking_revision", models.PositiveBigIntegerField(editable=False)),
                ("sequence", models.PositiveBigIntegerField(editable=False)),
                ("granted", models.BooleanField(editable=False)),
                ("text_version", models.CharField(max_length=48, editable=False)),
                ("language", models.CharField(max_length=2, editable=False)),
                ("text_snapshot", models.TextField(editable=False)),
                ("recorded_at", models.DateTimeField(auto_now_add=True)),
                (
                    "day_use_stay",
                    models.ForeignKey(
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        to="reservations.dayusestay",
                    ),
                ),
                (
                    "morning_stay",
                    models.ForeignKey(
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        to="reservations.morningstay",
                    ),
                ),
                (
                    "user",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT, to=settings.AUTH_USER_MODEL
                    ),
                ),
            ],
            options={
                "constraints": [
                    models.CheckConstraint(
                        condition=(
                            models.Q(morning_stay__isnull=False, day_use_stay__isnull=True)
                            | models.Q(morning_stay__isnull=True, day_use_stay__isnull=False)
                        ),
                        name="wa_choice_exactly_one_stay",
                    ),
                    models.UniqueConstraint(
                        fields=["morning_stay", "sequence"],
                        condition=models.Q(morning_stay__isnull=False),
                        name="wa_choice_morning_sequence",
                    ),
                    models.UniqueConstraint(
                        fields=["day_use_stay", "sequence"],
                        condition=models.Q(day_use_stay__isnull=False),
                        name="wa_choice_day_use_sequence",
                    ),
                ]
            },
        )
    ]
