from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("reservations", "0006_bookingintent_billing_address"),
    ]

    operations = [
        migrations.AlterField(
            model_name="bookingintent",
            name="status",
            field=models.CharField(
                choices=[
                    ("draft", "مسودة"),
                    ("pending_revalidation", "بانتظار إعادة التحقق"),
                    ("awaiting_payment", "جاهز للدفع"),
                    ("payment_verified", "تم التحقق من الدفع"),
                    ("price_changed", "تغير السعر"),
                    ("unavailable", "غير متاح"),
                    ("expired", "منتهي"),
                    ("cancelled", "ملغى"),
                    ("completed", "مكتمل"),
                ],
                default="draft",
                max_length=30,
            ),
        ),
    ]
