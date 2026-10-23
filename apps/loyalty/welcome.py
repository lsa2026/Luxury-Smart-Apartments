"""A single post-enrolment welcome for every entrance; never a history mailing."""

from datetime import timedelta
from importlib.resources import files
from urllib.parse import urljoin, urlsplit

from django.conf import settings
from django.core.exceptions import ValidationError
from django.utils import timezone

from apps.accounts.models import CustomerProfile
from apps.notifications.services.email import EmailProviderError, queue_email

from .models import GuestEvent

VERSION = "loyalty-welcome-20261023-v1"
COPY = {
    "ar": (
        "مرحبًا بك في برنامج الولاء",
        "سعداء بانضمامك إلى مكافآت ضيوفنا لدى Luxury Living. "
        "اكتملت عضويتك المجانية والاختيارية بموافقتك على شروط البرنامج. "
        "يمكنك متابعة نقاطك ومكافآت إقاماتك المؤهلة من حسابك، واستلام هدايا النقاط "
        "بعد توثيق جوالك. الانضمام وحده لا يضيف نقاطًا.",
        "رقم العضوية",
        "عرض حساب المكافآت",
    ),
    "en": (
        "Welcome to our loyalty programme",
        "We are delighted to welcome you to Luxury Living guest rewards. "
        "Your free, optional membership is active following your consent to the programme terms. "
        "View your points and eligible stay rewards in your account, and receive gifts of points "
        "after verifying your mobile. Joining alone does not add points.",
        "Membership number",
        "View rewards account",
    ),
    "fr": (
        "Bienvenue dans notre programme de fidélité",
        "Bienvenue dans les avantages Luxury Living. Votre adhésion gratuite et facultative "
        "est active après votre accord aux conditions du programme. Consultez vos points et "
        "les récompenses des séjours éligibles dans votre compte, et recevez des points en cadeau "
        "après vérification du mobile. L’adhésion seule ne crédite aucun point.",
        "Numéro d’adhésion",
        "Voir mon compte fidélité",
    ),
}


def image(language):
    if language not in COPY:
        raise ValidationError("Unsupported welcome language.")
    return (
        files("apps.loyalty")
        .joinpath("invitation_assets", f"welcome-{language}-v1.png")
        .read_bytes()
    )


def enabled():
    from .services import enabled as programme_enabled
    from .views import approved_terms

    # Transactional membership confirmation is independent of promotional invitations.
    return programme_enabled() and approved_terms() and settings.EMAIL_DELIVERY_ENABLED


def email_verified(customer):
    return bool(
        customer.email
        and CustomerProfile.objects.filter(user=customer, email_verified_at__isnull=False).exists()
    )


def queue(event):
    from .launch import participant_allowed

    if (
        not enabled()
        or event.kind != GuestEvent.Kind.JOIN
        or event.source == "history"
        or not event.member_id
        or event.member.status != "active"
        or not event.customer.is_active
        or not participant_allowed(event.customer)
        or not email_verified(event.customer)
    ):
        return None
    return queue_email(
        message_type="loyalty_welcome",
        recipient=event.customer.email,
        recipient_source="loyalty_welcome",
        recipient_reference=str(event.pk),
        language=event.member.language,
        idempotency_key=f"loyalty-welcome:{event.pk}",
    )


def resume_after_verification(customer):
    # Only a fresh join awaiting proof. No schedule, historical backfill or account welcome.
    event = (
        GuestEvent.objects.select_related("member", "customer")
        .filter(
            customer=customer,
            kind=GuestEvent.Kind.JOIN,
            source="guest",
            occurred_at__gte=timezone.now() - timedelta(days=1),
        )
        .first()
    )
    if event:
        return queue(event)


def context(member, language):
    from django.templatetags.static import static

    base = settings.SITE_BASE_URL.rstrip("/")
    parsed = urlsplit(base)
    if parsed.scheme != "https" or not parsed.netloc or parsed.username or parsed.password:
        raise ValidationError("Invalid welcome destination.")
    return {
        "first_name": member.customer.first_name,
        "membership_number": member.number,
        "membership_label": COPY[language][2],
        "action_label": COPY[language][3],
        "action_url": base + "/my-bookings/rewards/",
        "welcome_image_url": urljoin(base, static(f"images/loyalty/welcome-{language}-v1.png")),
        "welcome_image_alt": COPY[language][0],
    }


def resolve(delivery):
    from .launch import participant_allowed

    if not enabled():
        raise EmailProviderError("loyalty_welcome_disabled", permanent=True)
    event = GuestEvent.objects.select_related("member__customer", "customer").get(
        pk=delivery.recipient_reference
    )
    if (
        event.kind != GuestEvent.Kind.JOIN
        or event.source == "history"
        or not event.member_id
        or event.member.status != "active"
        or not event.customer.is_active
        or not participant_allowed(event.customer)
        or not email_verified(event.customer)
    ):
        raise EmailProviderError("loyalty_welcome_not_eligible", permanent=True)
    return event.customer.email, context(event.member, delivery.language)
