import uuid

from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        ("reservations", "0034_stay_extensions"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]
    operations = [
        migrations.CreateModel(
            name="TelegramActivation",
            fields=[
                (
                    "id",
                    models.UUIDField(
                        default=uuid.uuid4, editable=False, primary_key=True, serialize=False
                    ),
                ),
                ("state", models.CharField(default="processing", max_length=16)),
                ("proof_kind", models.CharField(max_length=20)),
                ("evidence_ref", models.CharField(blank=True, max_length=100)),
                ("phone_digest", models.CharField(max_length=64)),
                ("proof_id", models.UUIDField(default=uuid.uuid4, editable=False)),
                ("expires_at", models.DateTimeField(null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "actor",
                    models.ForeignKey(
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "reservation",
                    models.OneToOneField(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="telegram_activation",
                        to="reservations.reservation",
                    ),
                ),
            ],
        ),
    ]
