import uuid

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("reservations", "0033_persistent_admin_guest_files"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]
    operations = [
        migrations.CreateModel(
            name="StayExtensionChain",
            fields=[
                (
                    "id",
                    models.UUIDField(
                        primary_key=True, default=uuid.uuid4, editable=False, serialize=False
                    ),
                ),
                ("root_hostaway_reservation_id", models.PositiveBigIntegerField(unique=True)),
                ("root_snapshot", models.JSONField(default=dict)),
                ("revision", models.PositiveIntegerField(default=0)),
                ("fingerprint", models.CharField(max_length=64, blank=True)),
                ("review_required", models.BooleanField(default=False)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "property",
                    models.ForeignKey(
                        to="properties.property", on_delete=django.db.models.deletion.PROTECT
                    ),
                ),
            ],
        ),
        migrations.CreateModel(
            name="StayExtension",
            fields=[
                (
                    "id",
                    models.UUIDField(
                        primary_key=True, default=uuid.uuid4, editable=False, serialize=False
                    ),
                ),
                ("previous_hostaway_reservation_id", models.PositiveBigIntegerField()),
                ("previous_snapshot", models.JSONField(default=dict)),
                ("state", models.CharField(max_length=24, default="draft")),
                ("activation_generation", models.PositiveIntegerField(default=0)),
                ("ownership_acknowledged", models.BooleanField(default=False)),
                ("last_error", models.CharField(max_length=64, blank=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "chain",
                    models.ForeignKey(
                        to="reservations.stayextensionchain",
                        related_name="extensions",
                        on_delete=django.db.models.deletion.PROTECT,
                    ),
                ),
                (
                    "draft",
                    models.OneToOneField(
                        to="reservations.manualbookingdraft",
                        related_name="stay_extension",
                        on_delete=django.db.models.deletion.PROTECT,
                    ),
                ),
                (
                    "reservation",
                    models.OneToOneField(
                        to="reservations.reservation",
                        related_name="stay_extension",
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                    ),
                ),
                (
                    "created_by",
                    models.ForeignKey(
                        to=settings.AUTH_USER_MODEL, on_delete=django.db.models.deletion.PROTECT
                    ),
                ),
            ],
            options={
                "ordering": ["created_at", "id"],
                "constraints": [
                    models.UniqueConstraint(
                        fields=("previous_hostaway_reservation_id",),
                        condition=~models.Q(state="cancelled"),
                        name="extension_open_parent_unique",
                    )
                ],
            },
        ),
        migrations.CreateModel(
            name="StayExtensionEvent",
            fields=[
                (
                    "id",
                    models.UUIDField(
                        primary_key=True, default=uuid.uuid4, editable=False, serialize=False
                    ),
                ),
                ("revision", models.PositiveIntegerField()),
                ("kind", models.CharField(max_length=48)),
                ("state", models.CharField(max_length=16, default="pending")),
                ("attempts", models.PositiveIntegerField(default=0)),
                ("lease_until", models.DateTimeField(null=True, blank=True)),
                ("acknowledged_at", models.DateTimeField(null=True, blank=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "extension",
                    models.ForeignKey(
                        to="reservations.stayextension",
                        related_name="events",
                        on_delete=django.db.models.deletion.PROTECT,
                    ),
                ),
            ],
            options={
                "constraints": [
                    models.UniqueConstraint(
                        fields=("extension", "revision"), name="extension_event_revision_unique"
                    )
                ]
            },
        ),
    ]
