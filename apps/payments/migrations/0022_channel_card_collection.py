import uuid

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("payments", "0021_hostaway_charge_id_as_text"),
        ("properties", "0022_property_price_calendar"),
        ("reservations", "0034_stay_extensions"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]
    operations = [
        migrations.CreateModel(
            name="ChannelCardCollection",
            fields=[
                (
                    "id",
                    models.UUIDField(
                        primary_key=True, default=uuid.uuid4, editable=False, serialize=False
                    ),
                ),
                (
                    "hostaway_reservation_id",
                    models.CharField(max_length=32, unique=True, editable=False),
                ),
                ("booking_snapshot", models.JSONField(default=dict, editable=False)),
                ("amount", models.DecimalField(max_digits=12, decimal_places=2)),
                ("currency", models.CharField(max_length=3)),
                ("available_on", models.DateField()),
                ("charge_before", models.DateField()),
                ("balance_checked_at", models.DateTimeField()),
                ("payer", models.JSONField(default=dict, editable=False)),
                (
                    "status",
                    models.CharField(
                        max_length=16,
                        default="draft",
                        choices=[
                            ("draft", "بانتظار بدء التحصيل"),
                            ("created", "جارٍ تجهيز البوابة"),
                            ("pending", "بانتظار تأكيد البوابة"),
                            ("succeeded", "تم التحقق من القبض"),
                            ("failed", "رفضت البوابة العملية — يلزم مراجعة"),
                            ("cancelled", "ألغيت العملية — يلزم مراجعة"),
                            ("unknown", "نتيجة غير محسومة — لا تعد التحصيل"),
                            ("review", "يلزم مراجعة التحصيل"),
                        ],
                    ),
                ),
                ("environment", models.CharField(max_length=10, editable=False)),
                ("entity_fingerprint", models.CharField(max_length=64, blank=True, editable=False)),
                (
                    "approval_reference",
                    models.CharField(max_length=100, blank=True, editable=False),
                ),
                (
                    "merchant_transaction_id",
                    models.CharField(max_length=64, unique=True, editable=False),
                ),
                (
                    "provider_checkout_id",
                    models.CharField(
                        max_length=255, unique=True, null=True, blank=True, editable=False
                    ),
                ),
                ("widget_integrity", models.CharField(max_length=255, blank=True, editable=False)),
                (
                    "provider_payment_id",
                    models.CharField(
                        max_length=255, unique=True, null=True, blank=True, editable=False
                    ),
                ),
                (
                    "provider_result_code",
                    models.CharField(max_length=100, blank=True, editable=False),
                ),
                ("failure_code", models.CharField(max_length=100, blank=True, editable=False)),
                ("verified_at", models.DateTimeField(null=True, blank=True, editable=False)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "created_by",
                    models.ForeignKey(
                        to=settings.AUTH_USER_MODEL, on_delete=django.db.models.deletion.PROTECT
                    ),
                ),
                (
                    "property",
                    models.ForeignKey(
                        to="properties.property", on_delete=django.db.models.deletion.PROTECT
                    ),
                ),
                (
                    "reservation",
                    models.ForeignKey(
                        to="reservations.reservation",
                        on_delete=django.db.models.deletion.PROTECT,
                        null=True,
                        blank=True,
                    ),
                ),
            ],
            options={
                "ordering": ["-created_at"],
                "constraints": [
                    models.CheckConstraint(
                        condition=models.Q(amount__gt=0), name="channel_collection_positive"
                    ),
                    models.CheckConstraint(
                        condition=models.Q(currency="SAR"), name="channel_collection_sar_only"
                    ),
                    models.CheckConstraint(
                        condition=models.Q(charge_before__gt=models.F("available_on")),
                        name="channel_collection_charge_window",
                    ),
                ],
            },
        ),
    ]
