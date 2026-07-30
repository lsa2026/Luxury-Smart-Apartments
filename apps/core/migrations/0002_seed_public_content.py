from django.db import migrations


PAGES = [
    {
        "slug": "about",
        "title_ar": "من نحن",
        "title_en": "About us",
        "body_ar": (
            "Luxury Smart Apartments منصة محلية لعرض وحدات الإقامة اليومية "
            "والشهرية. نركز على تقديم معلومات واضحة عن الوحدات، مع التحقق "
            "المباشر من السعر والتوافر قبل متابعة طلب الحجز.\n\n"
            "هذه الصفحة قابلة للتحديث من لوحة الإدارة عند اعتماد المحتوى النهائي."
        ),
        "body_en": (
            "Luxury Smart Apartments is a local platform for daily and monthly "
            "stays. We focus on clear property information and live price and "
            "availability checks before a booking request continues.\n\n"
            "This page can be updated from the administration area when final "
            "brand content is approved."
        ),
    },
    {
        "slug": "terms",
        "title_ar": "الشروط والأحكام",
        "title_en": "Terms and conditions",
        "body_ar": (
            "هذه نسخة أولية للمراجعة القانونية. عرض السعر مؤقت، وطلب الحجز "
            "المبدئي لا يمثل حجزًا مؤكدًا. لا يصبح أي حجز مؤكدًا إلا بعد "
            "استكمال الخطوات التشغيلية التي ستعتمد لاحقًا."
        ),
        "body_en": (
            "This is an initial draft for legal review. A price quote is "
            "temporary, and a local booking request is not a confirmed booking. "
            "Confirmation requires the operational steps that will be approved later."
        ),
    },
    {
        "slug": "privacy",
        "title_ar": "سياسة الخصوصية",
        "title_en": "Privacy policy",
        "body_ar": (
            "نجمع فقط البيانات اللازمة للرد على رسائل التواصل وطلبات الحجز "
            "المبدئية. لا نجمع بيانات بطاقات الدفع في هذه المرحلة، ولا نعرض "
            "بيانات الضيوف للزوار. تحتاج هذه المسودة إلى مراجعة قانونية قبل الإطلاق."
        ),
        "body_en": (
            "We collect only the information needed to respond to contact "
            "messages and local booking requests. Payment card data is not "
            "collected at this stage, and guest data is not exposed publicly. "
            "This draft requires legal review before launch."
        ),
    },
    {
        "slug": "cancellation",
        "title_ar": "سياسة الإلغاء",
        "title_en": "Cancellation policy",
        "body_ar": (
            "سياسة الإلغاء والاسترداد النهائية لم تعتمد بعد. أي طلب إلغاء "
            "محلي يبقى قيد المراجعة ولا يغير الحجز لدى مصدره تلقائيًا، ولا "
            "ينشئ عملية استرداد."
        ),
        "body_en": (
            "The final cancellation and refund policy has not yet been approved. "
            "A local cancellation request remains under review, does not "
            "automatically change the source booking, and does not issue a refund."
        ),
    },
    {
        "slug": "cookies",
        "title_ar": "سياسة ملفات الارتباط",
        "title_en": "Cookie policy",
        "body_ar": (
            "يستخدم الموقع ملفات ارتباط أساسية للجلسة والأمان واختيار اللغة. "
            "لم تُفعّل أدوات التحليلات أو الإعلانات في هذه المرحلة. ستُحدّث "
            "هذه السياسة قبل تفعيل أي أدوات غير أساسية."
        ),
        "body_en": (
            "The site uses essential cookies for sessions, security, and language "
            "selection. Analytics and advertising tools are not enabled at this "
            "stage. This policy will be updated before non-essential tools are enabled."
        ),
    },
]

FAQS = [
    {
        "question_ar": "هل السعر المعروض نهائي؟",
        "question_en": "Is the displayed price final?",
        "answer_ar": (
            "السعر مؤقت ويأتي من نظام التشغيل، ويعاد التحقق منه قبل متابعة "
            "طلب الحجز. لم تُفعّل بوابة الدفع بعد."
        ),
        "answer_en": (
            "The price is temporary and comes from the operating system. It is "
            "checked again before a booking request continues. Payment is not enabled yet."
        ),
        "sort_order": 10,
    },
    {
        "question_ar": "هل إرسال بيانات الضيف يؤكد الحجز؟",
        "question_en": "Does submitting guest details confirm a booking?",
        "answer_ar": (
            "لا. الطلب المحلي غير مؤكد حتى اكتمال الدفع وإنشاء الحجز، وهاتان "
            "الخطوتان غير مفعّلتين في المرحلة الحالية."
        ),
        "answer_en": (
            "No. A local request is not confirmed until payment is completed and "
            "the booking is created. Both steps are disabled at the current stage."
        ),
        "sort_order": 20,
    },
    {
        "question_ar": "متى يظهر العنوان الكامل؟",
        "question_en": "When is the full address shared?",
        "answer_ar": (
            "لا نعرض العنوان الخاص كاملًا في الصفحات العامة. تفاصيل الوصول "
            "تُقدّم عبر مسار الحجز المؤكد عند تفعيله."
        ),
        "answer_en": (
            "The private full address is not shown on public pages. Arrival "
            "details will be provided through the confirmed booking flow when enabled."
        ),
        "sort_order": 30,
    },
]


def seed_content(apps, schema_editor):
    site_page = apps.get_model("core", "SitePage")
    faq_item = apps.get_model("core", "FAQItem")
    site_setting = apps.get_model("core", "SiteSetting")
    for page in PAGES:
        site_page.objects.update_or_create(slug=page["slug"], defaults=page)
    for faq in FAQS:
        faq_item.objects.get_or_create(
            question_en=faq["question_en"],
            defaults=faq,
        )
    site_setting.objects.get_or_create(site_name="Luxury Smart Apartments")


def unseed_content(apps, schema_editor):
    site_page = apps.get_model("core", "SitePage")
    faq_item = apps.get_model("core", "FAQItem")
    site_page.objects.filter(slug__in=[page["slug"] for page in PAGES]).delete()
    faq_item.objects.filter(question_en__in=[item["question_en"] for item in FAQS]).delete()


class Migration(migrations.Migration):
    dependencies = [("core", "0001_initial")]

    operations = [migrations.RunPython(seed_content, unseed_content)]
