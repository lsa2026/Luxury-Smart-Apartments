from django.db import migrations

CURRENT_QUESTION = "Where can I find the check-in and check-out times?"

UPDATED_VALUES = {
    "question_ar": "ما وقت تسجيل الدخول والخروج؟",
    "question_en": "What are the check-in and check-out times?",
    "question_fr": "Quels sont les horaires d’arrivée et de départ ?",
    "answer_ar": (
        "يبدأ تسجيل الدخول من الساعة 3:00 مساءً، ويكون تسجيل الخروج في موعد أقصاه "
        "الساعة 12:00 ظهرًا. يسعدنا النظر في طلبات الدخول المبكر أو الخروج المتأخر "
        "متى سمح مستوى الإشغال وجدول تجهيز الوحدة بذلك. خلال فترات الإشغال المرتفع "
        "نلتزم بالمواعيد الأساسية لضمان جاهزية الوحدة للضيف التالي، أما عند توفر وقت "
        "كافٍ فقد نتمكن من تقديم مرونة إضافية بعد تأكيد الطلب."
    ),
    "answer_en": (
        "Check-in begins at 3:00 PM, and check-out is by 12:00 PM. We are happy to "
        "consider early check-in or late check-out when occupancy and the property "
        "preparation schedule allow. During busy periods, we follow the standard times "
        "closely so the property is ready for the next guest. When sufficient time is "
        "available, we may be able to offer additional flexibility after confirming "
        "the request."
    ),
    "answer_fr": (
        "L’arrivée est possible à partir de 15 h et le départ doit avoir lieu au plus "
        "tard à 12 h. Nous étudions volontiers les demandes d’arrivée anticipée ou de "
        "départ tardif lorsque le taux d’occupation et le planning de préparation le "
        "permettent. En période de forte affluence, nous respectons les horaires standard "
        "afin que le logement soit prêt pour le voyageur suivant. Lorsqu’un délai suffisant "
        "est disponible, une certaine souplesse peut être accordée après confirmation de "
        "la demande."
    ),
}

PREVIOUS_VALUES = {
    "question_ar": "أين أجد وقت الدخول والخروج؟",
    "question_en": CURRENT_QUESTION,
    "question_fr": "Où trouver les horaires d’arrivée et de départ ?",
    "answer_ar": (
        "قد تختلف الأوقات حسب الوحدة. يظهر الوقت الخاص بالوحدة في صفحتها أو أثناء "
        "الحجز، ويُثبت في تأكيد الحجز وتعليمات الوصول. اعتمد دائمًا الوقت المذكور في "
        "تأكيد حجزك."
    ),
    "answer_en": (
        "Times may vary by property. The applicable time is shown on the property page or "
        "during booking and is recorded in your confirmation and arrival instructions. "
        "Always follow the time stated in your booking confirmation."
    ),
    "answer_fr": (
        "Les horaires peuvent varier selon le logement. L’horaire applicable est affiché "
        "sur sa page ou pendant la réservation, puis indiqué dans la confirmation et les "
        "instructions d’arrivée. Référez-vous toujours à votre confirmation."
    ),
}


def set_standard_times(apps, schema_editor):
    del schema_editor
    faq_item = apps.get_model("core", "FAQItem")
    faq_item.objects.filter(
        question_en=CURRENT_QUESTION,
        property__isnull=True,
    ).update(**UPDATED_VALUES)


def restore_previous_answer(apps, schema_editor):
    del schema_editor
    faq_item = apps.get_model("core", "FAQItem")
    faq_item.objects.filter(
        question_en=UPDATED_VALUES["question_en"],
        property__isnull=True,
    ).update(**PREVIOUS_VALUES)


class Migration(migrations.Migration):
    dependencies = [("core", "0019_expand_verified_public_faq")]

    operations = [
        migrations.RunPython(set_standard_times, restore_previous_answer),
    ]
