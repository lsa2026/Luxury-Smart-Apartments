"""Owner-approved public copy only; preserve custom content and all stay policies."""

from importlib import import_module

from django.db import migrations

PAGES = import_module("apps.core.migrations.0008_public_information_pages").PAGES
SETTINGS = import_module("apps.core.migrations.0008_public_information_pages").SITE_SETTINGS
FAQ = import_module("apps.core.migrations.0019_expand_verified_public_faq").EXISTING_FAQ_UPDATES[
    "When is the full address shared?"
]

ANSWERS = {
    "answer_ar": (
        "يمكنك الاطلاع على عنوان الشقة وموقعها على الخريطة في صفحة الوحدة قبل الحجز. "
        "بعد تأكيد الحجز واستيفاء متطلبات التحقق، نرسل إليك تعليمات الوصول وتفاصيل "
        "الدخول الخاصة بإقامتك. تُرسل رموز الدخول بشكل خاص ولا تُعرض علنًا، وقد يختلف "
        "توقيت إرسالها وطريقة الدخول حسب الوحدة."
    ),
    "answer_en": (
        "You can view the apartment address and map location on its property page before "
        "booking. After your booking is confirmed and verification requirements are met, "
        "we send arrival instructions and access details for your stay. Access codes are "
        "shared privately, never publicly; their delivery timing and the access method "
        "may vary by property."
    ),
    "answer_fr": (
        "Vous pouvez consulter l’adresse de l’appartement et son emplacement sur la carte "
        "sur la page du logement avant de réserver. Après confirmation de la réservation "
        "et vérification, nous vous envoyons les instructions d’arrivée et les modalités "
        "d’accès pour votre séjour. Les codes d’accès sont communiqués en privé, jamais "
        "publiquement ; leur moment d’envoi et le mode d’accès peuvent varier selon le logement."
    ),
}
PAGE_COPY = {
    "about": {
        "meta_description_ar": "تعرّف على شققنا وإقاماتنا الذكية للأعمال والعائلات في الرياض ومراكش.",
        "meta_description_en": "Discover our smart apartments and comfortable stays for business travellers and families in Riyadh and Marrakech.",
        "meta_description_fr": "Découvrez nos appartements et séjours intelligents pour les voyageurs d’affaires et les familles à Riyad et Marrakech.",
    },
    "contact": {
        "meta_description_ar": "تواصل مع فريقنا للاستفسار عن الشقق والحجوزات والإقامة في الرياض ومراكش.",
        "meta_description_en": "Contact our team for help with apartments, bookings and stays in Riyadh and Marrakech.",
        "meta_description_fr": "Contactez notre équipe pour vos questions sur les appartements, réservations et séjours à Riyad et Marrakech.",
    },
}
SETTING_COPY = {
    "tagline_en": "Luxury smart stays in Riyadh and Marrakech",
    "tagline_fr": "Séjours intelligents haut de gamme à Riyad et Marrakech",
    "footer_text_ar": "اكتشف إقامات راقية وذكية في الرياض بالمملكة العربية السعودية ومراكش بالمغرب.",
    "footer_text_en": "Discover refined smart stays in Riyadh, Saudi Arabia, and Marrakech, Morocco.",
    "footer_text_fr": "Découvrez des séjours raffinés et connectés à Riyad, en Arabie saoudite, et à Marrakech, au Maroc.",
}


def update_copy(apps, schema_editor, *, reverse=False):
    alias = schema_editor.connection.alias

    def replace(query, field, before, after):
        if reverse:
            before, after = after, before
        query.filter(**{field: before}).update(**{field: after})

    faqs = (
        apps.get_model("core", "FAQItem")
        .objects.using(alias)
        .filter(property__isnull=True, question_en=FAQ["question_en"])
    )
    for field, value in ANSWERS.items():
        replace(faqs, field, FAQ[field], value)
    pages = apps.get_model("core", "SitePage").objects.using(alias)
    for slug, values in PAGE_COPY.items():
        for field, value in values.items():
            replace(pages.filter(slug=slug), field, PAGES[slug][field], value)
    settings = apps.get_model("core", "SiteSetting").objects.using(alias)
    for field, value in SETTING_COPY.items():
        replace(settings, field, SETTINGS[field], value)


def forwards(apps, schema_editor):
    update_copy(apps, schema_editor)


def backwards(apps, schema_editor):
    update_copy(apps, schema_editor, reverse=True)


class Migration(migrations.Migration):
    dependencies = [("core", "0022_sitesetting_snapchat_url_sitesetting_tiktok_url_and_more")]
    operations = [migrations.RunPython(forwards, backwards)]
