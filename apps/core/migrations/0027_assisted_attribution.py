import uuid

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0026_public_business_emails"),
        ("reservations", "0034_stay_extensions"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]
    operations = [
        migrations.CreateModel(
            name="AttributionHandoff",
            fields=[
                (
                    "id",
                    models.UUIDField(
                        default=uuid.uuid4, editable=False, primary_key=True, serialize=False
                    ),
                ),
                ("reference_hash", models.CharField(editable=False, max_length=64, unique=True)),
                ("journey_hash", models.CharField(db_index=True, editable=False, max_length=64)),
                ("snapshot", models.JSONField(default=dict, editable=False)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("expires_at", models.DateTimeField()),
                ("revoked_at", models.DateTimeField(blank=True, null=True)),
            ],
        ),
        migrations.CreateModel(
            name="BookingAttribution",
            fields=[
                (
                    "id",
                    models.UUIDField(
                        default=uuid.uuid4, editable=False, primary_key=True, serialize=False
                    ),
                ),
                (
                    "journey_hash",
                    models.CharField(blank=True, db_index=True, editable=False, max_length=64),
                ),
                ("snapshot", models.JSONField(default=dict, editable=False)),
                ("linkage", models.CharField(max_length=20)),
                ("closure_method", models.CharField(max_length=20)),
                ("staff_source", models.CharField(blank=True, max_length=30)),
                ("revoked_at", models.DateTimeField(blank=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "quote",
                    models.OneToOneField(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="advertising_attribution",
                        to="reservations.bookingquote",
                    ),
                ),
                (
                    "morning_stay",
                    models.OneToOneField(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="advertising_attribution",
                        to="reservations.morningstay",
                    ),
                ),
                (
                    "day_use_stay",
                    models.OneToOneField(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="advertising_attribution",
                        to="reservations.dayusestay",
                    ),
                ),
                (
                    "handoff",
                    models.OneToOneField(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="binding",
                        to="core.attributionhandoff",
                    ),
                ),
                (
                    "recorded_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={
                "constraints": [
                    models.CheckConstraint(
                        condition=(
                            models.Q(
                                quote__isnull=False,
                                morning_stay__isnull=True,
                                day_use_stay__isnull=True,
                            )
                            | models.Q(
                                quote__isnull=True,
                                morning_stay__isnull=False,
                                day_use_stay__isnull=True,
                            )
                            | models.Q(
                                quote__isnull=True,
                                morning_stay__isnull=True,
                                day_use_stay__isnull=False,
                            )
                        ),
                        name="attribution_one_booking_anchor",
                    )
                ]
            },
        ),
    ]
