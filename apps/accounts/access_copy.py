"""Guest-access presentation only; no membership or authentication decisions."""
# Translated sentences stay intact for copy review.
# ruff: noqa: E501

from django.utils.translation import get_language

COPY = {
    "ar": {
        "eyebrow": "حساب الضيف",
        "heading": "حجوزاتك ومكافآتك، في مكان واحد",
        "intro": "تابع إقامتك ومكافآتك من حساب واحد، بسهولة ووضوح.",
        "bookings": "إدارة الحجوزات",
        "rewards": "مكافآت الولاء",
        "secure": "دخول آمن",
        "note": "لا يلزم حجز سابق لإنشاء حسابك. تظهر المكافآت عند إتاحتها لحسابك.",
        "verify_eyebrow": "تأكيد البريد الإلكتروني",
        "verify_hint": "خطوة واحدة للوصول إلى حجوزاتك ومكافآتك.",
        "change_email": "استخدام بريد آخر",
        "address_disclosure": "اختياري: يُرسل ما تكتبه في البحث إلى جوجل لاقتراح العنوان. يمكنك تعبئة الحقول يدويًا دون البحث.",
        "choose_country": "اختر الدولة — اختياري",
    },
    "en": {
        "eyebrow": "Your guest account",
        "heading": "Your bookings and rewards, together",
        "intro": "One simple place to manage your stays and keep up with your rewards.",
        "bookings": "Manage bookings",
        "rewards": "Loyalty rewards",
        "secure": "Secure access",
        "note": "No previous booking is needed to create an account. Rewards appear when available for your account.",
        "verify_eyebrow": "Confirm your email",
        "verify_hint": "One step to your bookings and rewards.",
        "change_email": "Use another email",
        "address_disclosure": "Optional: what you type in this search is sent to Google for address suggestions. You can enter the fields manually without searching.",
        "choose_country": "Choose a country — optional",
    },
    "fr": {
        "eyebrow": "Votre espace personnel",
        "heading": "Vos réservations et vos avantages réunis",
        "intro": "Un seul espace pour gérer vos séjours et retrouver vos avantages.",
        "bookings": "Gérer mes réservations",
        "rewards": "Avantages fidélité",
        "secure": "Accès sécurisé",
        "note": "Aucune réservation antérieure n’est nécessaire pour créer un compte. Les avantages apparaissent lorsqu’ils sont disponibles pour votre compte.",
        "verify_eyebrow": "Confirmez votre e-mail",
        "verify_hint": "Une étape pour retrouver vos réservations et vos avantages.",
        "change_email": "Utiliser un autre e-mail",
        "address_disclosure": "Facultatif : le texte saisi dans cette recherche est envoyé à Google pour suggérer une adresse. Vous pouvez remplir les champs manuellement sans recherche.",
        "choose_country": "Choisir un pays — facultatif",
    },
}


def access_copy():
    return COPY.get((get_language() or "ar").split("-")[0], COPY["ar"])
