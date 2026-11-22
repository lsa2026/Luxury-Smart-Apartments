"""Private activation UI in the site's three guest languages."""

from django.utils.translation import get_language

UI = {
    "ar": {
        "title": "تفعيل تليجرام للحجز",
        "button": "تجهيز رابط التفعيل",
        "open": "فتح البوت وإكمال التفعيل",
        "expiry": "تنتهي صلاحية الرابط",
        "private": "خاص بهذا الحجز فقط. لا يتم إرسال رسالة أو تعديل الحجز عند تجهيز الرابط.",
        "once": "لن يُعرض الرابط مرة أخرى بعد مغادرة هذه الصفحة. لا تشاركه مع شخص آخر.",
        "notice": "تجهيز الرابط لا يعد إتمامًا للربط أو قبولًا لشروط الولاء أو تأكيدًا للدفع.",
    },
    "en": {
        "title": "Activate Telegram for this booking",
        "button": "Prepare activation link",
        "open": "Open the bot and verify",
        "expiry": "Link expires",
        "private": "This booking only. Preparing this link sends no message or booking change.",
        "once": "Shown once. Keep this link private.",
        "notice": "This link does not verify identity, accept loyalty terms or confirm payment.",
    },
    "fr": {
        "title": "Activer Telegram pour cette réservation",
        "button": "Préparer le lien",
        "open": "Ouvrir le bot et vérifier",
        "expiry": "Expiration du lien",
        "private": "Pour cette réservation uniquement. Aucun message ni changement de réservation.",
        "once": "Le lien ne sera plus affiché après votre départ. Ne le partagez pas.",
        "notice": "Ce lien ne valide ni la liaison, ni les conditions de fidélité, ni le paiement.",
    },
}
STATES = {
    "en": {
        "disabled": "Telegram activation is currently unavailable.",
        "unverified": "We must verify that the phone belongs to the owner of this booking first.",
        "missing": "No verified phone. Contact management through your booking channel.",
        "active": "A valid link was already prepared. Use the copy shown when it was issued.",
        "expired": "The previous link expired. Request a new one after verification.",
        "bound": "Telegram is already linked to this booking.",
        "unavailable": "Status not confirmed. Ask management to review before trying again.",
        "uncertain": "The result is uncertain. Repetition is blocked until management reviews it.",
        "processing": "Activation is processing. Do not repeat this request.",
        "hold": "No link was issued. Management must review the phone proof and booking status.",
        "issued": "Link ready. Open the bot and share your own contact to verify.",
        "closed": "Activation is unavailable for this booking.",
    },
    "fr": {
        "disabled": "L’activation Telegram est indisponible pour le moment.",
        "unverified": "Le numéro doit être vérifié pour le titulaire de cette réservation.",
        "missing": "Numéro non vérifié. Contactez la gestion via votre plateforme.",
        "active": "Un lien valide existe déjà. Utilisez la copie affichée lors de sa création.",
        "expired": "Le lien précédent a expiré. Une nouvelle vérification est nécessaire.",
        "bound": "Telegram est déjà lié à cette réservation.",
        "unavailable": "Statut non confirmé. Contactez la gestion avant de réessayer.",
        "uncertain": "Résultat incertain. Une vérification est requise avant de réessayer.",
        "processing": "Activation en cours. Ne répétez pas la demande.",
        "hold": "Aucun lien créé. La gestion doit vérifier le numéro et la réservation.",
        "issued": "Lien prêt. Ouvrez le bot et partagez votre propre contact pour terminer.",
        "closed": "Activation indisponible pour cette réservation.",
    },
}


def copy_for(request):
    language = "ar" if request.path.startswith("/admin/") else (get_language() or "ar")[:2]
    return language if language in UI else "en"
