"""Fixed, language-specific artwork; no guest information is baked into images."""

from importlib.resources import files

from django.core.exceptions import ValidationError

from .launch import pilot_only

TEMPLATE_VERSION = "gift-whatsapp-20261016-v2"
LANGUAGES = {"ar", "en", "fr"}


def invitation_image(language):
    if language not in LANGUAGES:
        raise ValidationError("Unsupported invitation language.")
    return (
        files("apps.loyalty")
        .joinpath("invitation_assets", f"invitation-{language}-v2.png")
        .read_bytes()
    )


def invitation_caption(name, *, language, url):
    title, intro, steps, action, note, trial = {
        "ar": (
            "*دعوة خاصة إلى مكافآت ضيوفنا*",
            f"أهلًا بك،\n{name} يدعوك للانضمام إلى برنامج الولاء لدى لاكشري ليفينغ.",
            "انضم مجانًا، ووثّق جوالك لتتمكن من استلام هدايا النقاط من أصدقائك، "
            "حتى إن لم يسبق لك الحجز معنا.",
            "*ابدأ من هنا:*",
            "الانضمام اختياري. هذه دعوة فقط، ولم تُحوّل أو تُحجز لك نقاط. "
            "إن لم تكن تتوقعها، يمكنك تجاهلها.",
            "البرنامج حاليًا تجربة خاصة للحسابات المعتمدة فقط.",
        ),
        "en": (
            "*A personal invitation to guest rewards*",
            f"Welcome,\n{name} invites you to join the Luxury Living rewards programme.",
            "Join for free and verify your mobile to receive gifts of points from friends. "
            "No previous booking is needed to join.",
            "*Your invitation:*",
            "Joining is optional. No points have been sent or reserved. "
            "If you were not expecting this invitation, you can ignore it.",
            "The programme is currently a private trial for approved accounts only.",
        ),
        "fr": (
            "*Une invitation personnelle à nos avantages*",
            f"Bienvenue,\n{name} vous invite à rejoindre le programme de fidélité Luxury Living.",
            "Adhérez gratuitement et vérifiez votre mobile pour recevoir des points en cadeau "
            "de vos proches. Aucun séjour préalable n’est nécessaire pour adhérer.",
            "*Votre invitation :*",
            "L’adhésion est facultative. Aucun point n’a été transféré ni réservé. "
            "Si vous n’attendiez pas cette invitation, vous pouvez l’ignorer.",
            "Le programme est actuellement un essai privé réservé aux comptes approuvés.",
        ),
    }[language]
    sections = [title, intro, steps, action + "\n" + url, note]
    if pilot_only():
        sections.append(trial)
    caption = "\n\n".join(sections)
    if len(caption.encode("utf-16-le")) // 2 > 1024:
        raise ValidationError("Invitation caption exceeds the provider limit.")
    return caption
