from django.db import migrations


UPDATED_FAQS = {
    "Is the displayed price final?": {
        "answer_ar": (
            "السعر مؤقت ويأتي من نظام التشغيل، ويُعاد التحقق من السعر والتوافر "
            "قبل الدفع. قد تتاح بوابة الدفع بحسب العملة ومزوّد الخدمة."
        ),
        "answer_en": (
            "The price is temporary and comes from the operating system. Price and "
            "availability are checked again before payment. Payment availability may "
            "depend on the currency and provider."
        ),
        "answer_fr": (
            "Le prix est temporaire et provient du système d’exploitation. Le prix et "
            "la disponibilité sont vérifiés à nouveau avant le paiement. La disponibilité "
            "du paiement peut dépendre de la devise et du prestataire."
        ),
    },
    "Does submitting guest details confirm a booking?": {
        "answer_ar": (
            "لا. يصبح الطلب مؤكدًا فقط بعد التحقق من الدفع وتأكيد الحجز تشغيليًا. "
            "إذا نجح الدفع وبقي التأكيد قيد المراجعة، فلا تدفع مرة أخرى وتواصل معنا."
        ),
        "answer_en": (
            "No. A request is confirmed only after payment is verified and the booking "
            "is operationally confirmed. If payment succeeds while confirmation remains "
            "under review, do not pay again; contact us."
        ),
        "answer_fr": (
            "Non. Une demande n’est confirmée qu’après la vérification du paiement et la "
            "confirmation opérationnelle de la réservation. Si le paiement réussit mais "
            "que la confirmation reste en cours d’examen, ne payez pas de nouveau et "
            "contactez-nous."
        ),
    },
}


def refresh_booking_faq(apps, schema_editor):
    del schema_editor
    faq_item = apps.get_model("core", "FAQItem")
    for question_en, values in UPDATED_FAQS.items():
        faq_item.objects.filter(question_en=question_en).update(**values)


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0009_english_brand_name_only"),
    ]

    operations = [
        migrations.RunPython(refresh_booking_faq, migrations.RunPython.noop),
    ]
