from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("payments", "0003_paymentattempt_modification_request")]

    operations = [
        migrations.AddField(
            model_name="paymentattempt",
            name="merchant_transaction_id",
            field=models.CharField(
                blank=True, editable=False, max_length=255, null=True, unique=True
            ),
        ),
        migrations.AddField(
            model_name="paymentattempt",
            name="provider_checkout_id",
            field=models.CharField(
                blank=True, editable=False, max_length=255, null=True, unique=True
            ),
        ),
        migrations.AddField(
            model_name="paymentattempt",
            name="provider_payment_id",
            field=models.CharField(
                blank=True, editable=False, max_length=255, null=True, unique=True
            ),
        ),
        migrations.AddField(
            model_name="paymentattempt",
            name="provider_result_code",
            field=models.CharField(blank=True, editable=False, max_length=100),
        ),
        migrations.AddField(
            model_name="paymentattempt",
            name="provider_result_description",
            field=models.CharField(blank=True, editable=False, max_length=255),
        ),
        migrations.AddField(
            model_name="paymentattempt",
            name="verified_at",
            field=models.DateTimeField(blank=True, editable=False, null=True),
        ),
        migrations.AddField(
            model_name="paymentattempt",
            name="widget_integrity",
            field=models.CharField(blank=True, editable=False, max_length=255),
        ),
        migrations.AlterField(
            model_name="paymentattempt",
            name="status",
            field=models.CharField(
                choices=[
                    ("created", "منشأة"),
                    ("pending", "قيد المعالجة"),
                    ("succeeded", "ناجحة"),
                    ("failed", "فاشلة"),
                    ("cancelled", "ملغاة"),
                    ("expired", "منتهية"),
                    ("refunded", "مستردة"),
                    ("partially_refunded", "مستردة جزئيًا"),
                    ("review", "تحتاج مراجعة"),
                    ("unknown", "غير معروفة"),
                ],
                default="created",
                max_length=30,
            ),
        ),
    ]
