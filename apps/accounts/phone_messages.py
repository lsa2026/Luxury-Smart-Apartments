"""One-language authentication copy and the unchanged approved brand artwork."""

from importlib.resources import files

from apps.payments.payment_link_tasks import PAYMENT_MESSAGE_BRAND

AUTH_MARKER = "[LSA-AUTH]"


def phone_logo():
    # The same approved square light monogram used in invoices. No OTP or guest
    # data is baked into artwork, filenames or public media URLs.
    return files("apps.notifications").joinpath("invoice_assets", "logo.png").read_bytes()


AUTH_TITLES = {
    "ar": "🔐 *رمز الدخول الآمن*",
    "en": "🔐 *Secure sign-in code*",
    "fr": "🔐 *Code de connexion sécurisé*",
}


def supported_language(value):
    value = str(value or "").strip().lower().replace("_", "-").split("-")[0]
    return value if value in AUTH_TITLES else ""


def phone_message(*, code, language, purpose):
    """No links, booking facts, names or language duplication in a security code."""
    language = supported_language(language) or "ar"
    greeting, intro, expiry, security, ignore, thanks = {
        "ar": (
            "أهلًا بك،",
            "لتوثيق جوالك، أدخل الرمز التالي في الموقع:"
            if purpose == "bind"
            else "للدخول إلى حسابك، أدخل الرمز التالي في الموقع:",
            "صالح لمدة خمس دقائق ولمرة واحدة.",
            "لحماية حسابك، لا تشارك هذا الرمز مع أي شخص، حتى فريقنا.",
            "إذا لم تطلب الرمز، تجاهل هذه الرسالة.",
            f"شكرًا لاختيارك {PAYMENT_MESSAGE_BRAND}.",
        ),
        "en": (
            "Welcome,",
            "To verify your mobile, enter this code on our website:"
            if purpose == "bind"
            else "To sign in to your account, enter this code on our website:",
            "Valid for five minutes and one use only.",
            "Keep your account safe. Never share this code with anyone, including our team.",
            "If you did not request this code, ignore this message.",
            f"Thank you for choosing {PAYMENT_MESSAGE_BRAND}.",
        ),
        "fr": (
            "Bienvenue,",
            "Pour vérifier votre mobile, saisissez ce code sur notre site :"
            if purpose == "bind"
            else "Pour accéder à votre compte, saisissez ce code sur notre site :",
            "Valable cinq minutes et une seule fois.",
            "Pour protéger votre compte, ne partagez ce code avec personne, même notre équipe.",
            "Si vous n’avez pas demandé ce code, ignorez ce message.",
            f"Merci d’avoir choisi {PAYMENT_MESSAGE_BRAND}.",
        ),
    }[language]
    # Keep the existing SMA archive/model exclusion contract in every language.
    return "\n\n".join(
        [
            f"✨ *{PAYMENT_MESSAGE_BRAND}*",
            greeting,
            AUTH_TITLES[language],
            intro,
            f"*{code}*",
            f"{expiry}\n{security}",
            ignore,
            thanks,
            AUTH_MARKER,
        ]
    )
