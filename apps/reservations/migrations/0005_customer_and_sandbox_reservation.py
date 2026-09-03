from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ("reservations", "0004_bookingintent_language"),
    ]

    operations = [
        migrations.AddField(
            model_name="bookingintent",
            name="customer",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="booking_intents",
                to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.AddField(
            model_name="reservation",
            name="is_test",
            field=models.BooleanField(default=False, editable=False),
        ),
        migrations.RemoveConstraint(
            model_name="reservation",
            name="reservation_confirmed_requires_hostaway_id",
        ),
        migrations.AddConstraint(
            model_name="reservation",
            constraint=models.CheckConstraint(
                condition=(
                    ~models.Q(("normalized_status", "confirmed"))
                    | models.Q(("hostaway_reservation_id__isnull", False))
                    | models.Q(("is_test", True))
                ),
                name="reservation_confirmed_requires_hostaway_id",
            ),
        ),
    ]
