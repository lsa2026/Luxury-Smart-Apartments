from django.db import migrations
from django.utils import timezone


def backfill_verified_booking_state(apps, schema_editor):
    del schema_editor
    payment_attempt = apps.get_model("payments", "PaymentAttempt")
    booking_intent = apps.get_model("reservations", "BookingIntent")
    reservation = apps.get_model("reservations", "Reservation")
    now = timezone.now()

    intent_ids = payment_attempt.objects.filter(
        provider="hyperpay",
        status="succeeded",
        modification_request__isnull=True,
    ).values_list("booking_intent_id", flat=True)

    reservation.objects.filter(
        booking_intent_id__in=intent_ids,
        normalized_status="awaiting_payment",
    ).update(
        normalized_status="ready_for_hostaway",
        payment_status="paid",
        updated_at=now,
    )
    reservation.objects.filter(
        booking_intent_id__in=intent_ids,
        payment_status="",
    ).exclude(normalized_status="awaiting_payment").update(
        payment_status="paid",
        updated_at=now,
    )
    booking_intent.objects.filter(
        pk__in=intent_ids,
        status="awaiting_payment",
    ).update(
        status="payment_verified",
        updated_at=now,
    )


class Migration(migrations.Migration):
    dependencies = [
        ("payments", "0004_hyperpay_payment_fields"),
        ("reservations", "0007_bookingintent_payment_verified_status"),
    ]

    operations = [
        migrations.RunPython(
            backfill_verified_booking_state,
            migrations.RunPython.noop,
        ),
    ]
