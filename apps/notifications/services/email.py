"""Provider-neutral, privacy-conscious email delivery."""

import logging
from dataclasses import dataclass, field
from smtplib import SMTPException, SMTPRecipientsRefused
from typing import Any, Protocol
from urllib.parse import urljoin

from django.conf import settings
from django.core.mail import BadHeaderError, EmailMultiAlternatives
from django.db import DatabaseError, transaction
from django.template.loader import render_to_string
from django.utils import timezone
from django.utils.crypto import salted_hmac
from django.utils.html import strip_tags

from apps.notifications.models import EmailDelivery
from apps.reservations.security import mask_email

logger = logging.getLogger(__name__)


class EmailProviderError(Exception):
    def __init__(self, code: str, *, permanent: bool = False) -> None:
        super().__init__(code)
        self.code = code
        self.permanent = permanent


@dataclass(frozen=True, slots=True)
class EmailMessageRequest:
    recipient: str
    subject: str
    template_name: str
    language: str
    context: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class EmailSendResult:
    sent: bool
    code: str
    provider_message_id: str | None = None


class EmailProvider(Protocol):
    name: str

    def send(self, request: EmailMessageRequest) -> EmailSendResult: ...


class DisabledEmailProvider:
    name = "disabled"

    def send(self, request: EmailMessageRequest) -> EmailSendResult:
        del request
        return EmailSendResult(sent=False, code="email_delivery_disabled")


class DjangoEmailProvider:
    name = "django"

    def send(self, request: EmailMessageRequest) -> EmailSendResult:
        template_root = f"emails/{request.language}/{request.template_name}"
        context = {
            **request.context,
            **_email_brand_context(request.language),
        }
        try:
            text_body = render_to_string(f"emails/{request.language}/message.txt", context)
            html_body = render_to_string(f"{template_root}.html", context)
            message = EmailMultiAlternatives(
                subject=request.subject,
                body=text_body,
                from_email=settings.DEFAULT_FROM_EMAIL,
                to=[request.recipient],
                reply_to=[settings.EMAIL_REPLY_TO] if settings.EMAIL_REPLY_TO else None,
            )
            message.attach_alternative(html_body, "text/html")
            sent_count = message.send(fail_silently=False)
        except (BadHeaderError, SMTPRecipientsRefused, ValueError) as exc:
            raise EmailProviderError("invalid_email_message", permanent=True) from exc
        except (TimeoutError, OSError, SMTPException) as exc:
            raise EmailProviderError("email_provider_unavailable") from exc
        if sent_count != 1:
            raise EmailProviderError("email_not_accepted")
        return EmailSendResult(sent=True, code="sent")


def _email_brand_context(language: str) -> dict[str, str]:
    """Resolve public brand details at send time without storing them in queue rows."""
    del language
    site_setting = None
    try:
        from apps.core.branding import BRAND_NAME
        from apps.core.models import SiteSetting

        site_setting = SiteSetting.objects.order_by("pk").first()
    except DatabaseError:
        logger.warning("Email brand settings unavailable; using environment fallbacks")

    brand_name = BRAND_NAME
    contact_phone = settings.EMAIL_CONTACT_PHONE
    if site_setting is not None:
        contact_phone = (
            site_setting.contact_phone
            or site_setting.whatsapp_display_number
            or contact_phone
        )
    site_url = f"{settings.SITE_BASE_URL.rstrip('/')}/"
    configured_logo = settings.EMAIL_LOGO_URL.strip()
    logo_url = urljoin(site_url, configured_logo or "static/images/logo.jpeg")
    return {
        "brand_name": brand_name,
        "site_base_url": settings.SITE_BASE_URL.rstrip("/"),
        "site_url": site_url,
        "logo_url": logo_url,
        "contact_phone": contact_phone,
        "support_email": settings.SUPPORT_EMAIL,
    }


SUBJECTS: dict[str, dict[str, str]] = {
    "contact_confirmation": {
        "ar": "استلمنا رسالتك",
        "en": "We received your message",
        "fr": "Nous avons reçu votre message",
    },
    "contact_admin_alert": {
        "ar": "رسالة تواصل جديدة",
        "en": "New contact message",
        "fr": "Nouveau message de contact",
    },
    "booking_intent_created": {
        "ar": "تم إنشاء طلب حجز مبدئي",
        "en": "Your booking request was created",
        "fr": "Votre demande de réservation a été créée",
    },
    "booking_quote_expired": {
        "ar": "انتهت صلاحية عرض السعر",
        "en": "Your quote has expired",
        "fr": "Votre devis a expiré",
    },
    "booking_price_changed": {
        "ar": "تغير سعر طلب الحجز",
        "en": "Your booking price changed",
        "fr": "Le prix de votre réservation a changé",
    },
    "booking_unavailable": {
        "ar": "الوحدة لم تعد متاحة",
        "en": "The property is no longer available",
        "fr": "Le logement n’est plus disponible",
    },
    "booking_ready_for_payment": {
        "ar": "طلبك جاهز لمرحلة الدفع مستقبلًا",
        "en": "Your request is ready for the future payment step",
        "fr": "Votre demande est prête pour la future étape de paiement",
    },
    "extension_received": {
        "ar": "استلمنا طلب التمديد",
        "en": "Extension request received",
        "fr": "Demande de prolongation reçue",
    },
    "date_change_received": {
        "ar": "استلمنا طلب تغيير التواريخ",
        "en": "Date change request received",
        "fr": "Demande de changement de dates reçue",
    },
    "guest_change_received": {
        "ar": "استلمنا طلب تغيير الضيوف",
        "en": "Guest change request received",
        "fr": "Demande de modification des voyageurs reçue",
    },
    "cancellation_received": {
        "ar": "استلمنا طلب الإلغاء",
        "en": "Cancellation request received",
        "fr": "Demande d’annulation reçue",
    },
    "modification_approved": {
        "ar": "قُبل الطلب إداريًا",
        "en": "Request approved by operations",
        "fr": "Demande approuvée par les opérations",
    },
    "modification_rejected": {
        "ar": "رُفض الطلب",
        "en": "Request declined",
        "fr": "Demande refusée",
    },
    "modification_price_changed": {
        "ar": "تغير فرق السعر",
        "en": "Modification price difference changed",
        "fr": "La différence de prix de la modification a changé",
    },
    "payment_succeeded": {
        "ar": "تم استلام الدفع",
        "en": "Payment received",
        "fr": "Paiement reçu",
    },
    "reservation_confirmed": {
        "ar": "تم تأكيد الحجز",
        "en": "Reservation confirmed",
        "fr": "Réservation confirmée",
    },
    "reservation_creation_failed": {
        "ar": "تعذر إنشاء الحجز",
        "en": "Reservation creation failed",
        "fr": "Échec de la création de la réservation",
    },
    "reservation_unknown": {
        "ar": "حالة الحجز قيد المراجعة",
        "en": "Reservation status under review",
        "fr": "Statut de la réservation en cours d’examen",
    },
    "reservation_cancelled": {
        "ar": "تم إلغاء الحجز",
        "en": "Reservation cancelled",
        "fr": "Réservation annulée",
    },
    "reservation_modified": {
        "ar": "تم تعديل الحجز",
        "en": "Reservation updated",
        "fr": "Réservation mise à jour",
    },
    "account_verify_email": {
        "ar": "أكّد بريدك الإلكتروني",
        "en": "Confirm your email address",
        "fr": "Confirmez votre adresse e-mail",
    },
    "account_password_reset": {
        "ar": "إعادة تعيين كلمة المرور",
        "en": "Reset your password",
        "fr": "Réinitialiser votre mot de passe",
    },
    "account_welcome": {
        "ar": "أهلًا بك في عائلتنا",
        "en": "Welcome to our family",
        "fr": "Bienvenue dans notre famille",
    },
    "daily_operations_summary": {
        "ar": "ملخص العمليات اليومي",
        "en": "Daily operations summary",
        "fr": "Résumé quotidien des opérations",
    },
}

TEMPLATE_GROUPS = {
    "contact_confirmation": "contact",
    "contact_admin_alert": "contact",
    "booking_intent_created": "booking",
    "booking_quote_expired": "booking",
    "booking_price_changed": "booking",
    "booking_unavailable": "booking",
    "booking_ready_for_payment": "booking",
    "extension_received": "modification",
    "date_change_received": "modification",
    "guest_change_received": "modification",
    "cancellation_received": "modification",
    "modification_approved": "modification",
    "modification_rejected": "modification",
    "modification_price_changed": "modification",
    "payment_succeeded": "reservation",
    "reservation_confirmed": "reservation",
    "reservation_creation_failed": "reservation",
    "reservation_unknown": "reservation",
    "reservation_cancelled": "reservation",
    "reservation_modified": "reservation",
    "daily_operations_summary": "operations",
    "account_verify_email": "account",
    "account_password_reset": "account",
    "account_welcome": "account",
}

MESSAGES: dict[str, dict[str, str]] = {
    "contact_confirmation": {
        "ar": "شكرًا لتواصلك معنا. استلم فريقنا رسالتك وسيراجعها.",
        "en": "Thank you for contacting us. Our team has received your message.",
        "fr": "Merci de nous avoir contactés. Notre équipe a bien reçu votre message.",
    },
    "contact_admin_alert": {
        "ar": "توجد رسالة جديدة في صندوق التواصل. افتح لوحة الإدارة لمراجعتها.",
        "en": "A new message is available in the contact inbox.",
        "fr": "Un nouveau message est disponible dans la boîte de réception des contacts.",
    },
    "booking_intent_created": {
        "ar": "تم إنشاء طلبك المبدئي. هذا ليس حجزًا مؤكدًا ولم تُنفذ عملية دفع.",
        "en": "Your preliminary request was created. It is not confirmed or paid.",
        "fr": "Votre demande préliminaire a été créée. Elle n’est ni confirmée ni payée.",
    },
    "booking_quote_expired": {
        "ar": "انتهت صلاحية عرض السعر. أعد التحقق للحصول على سعر وتوافر حديثين.",
        "en": "Your quote expired. Recheck for current price and availability.",
        "fr": "Votre devis a expiré. Vérifiez à nouveau le prix et les disponibilités.",
    },
    "booking_price_changed": {
        "ar": "تغير السعر أثناء إعادة التحقق، ونحتاج إلى موافقتك على العرض الجديد.",
        "en": "The price changed during revalidation and requires your approval.",
        "fr": "Le prix a changé lors de la vérification et nécessite votre approbation.",
    },
    "booking_unavailable": {
        "ar": "لم تعد الوحدة متاحة للفترة المحددة. لم يُنشأ أي حجز.",
        "en": "The property is no longer available for those dates. No reservation was made.",
        "fr": "Le logement n’est plus disponible à ces dates. Aucune réservation n’a été créée.",
    },
    "booking_ready_for_payment": {
        "ar": "الطلب جاهز تقنيًا لمرحلة الدفع عند تفعيلها مستقبلًا، لكنه غير مؤكد.",
        "en": "The request is ready for the future payment step but is not confirmed.",
        "fr": (
            "La demande est prête pour la future étape de paiement, mais elle n’est pas confirmée."
        ),
    },
    "extension_received": {
        "ar": "استلمنا طلب تمديد الإقامة وهو قيد المراجعة. لم يتغير الحجز بعد.",
        "en": "We received the extension request. The reservation is unchanged.",
        "fr": "Nous avons reçu la demande de prolongation. La réservation reste inchangée.",
    },
    "date_change_received": {
        "ar": "استلمنا طلب تغيير التواريخ. لم يتغير الحجز بعد.",
        "en": "We received the date-change request. The reservation is unchanged.",
        "fr": "Nous avons reçu la demande de changement de dates. La réservation reste inchangée.",
    },
    "guest_change_received": {
        "ar": "استلمنا طلب تغيير عدد الضيوف. لم يتغير الحجز بعد.",
        "en": "We received the guest-change request. The reservation is unchanged.",
        "fr": (
            "Nous avons reçu la demande de modification des voyageurs. "
            "La réservation reste inchangée."
        ),
    },
    "cancellation_received": {
        "ar": "استلمنا طلب الإلغاء، لكن الحجز لم يُلغ بعد ولم يُنفذ استرداد.",
        "en": "We received the cancellation request; no cancellation or refund occurred.",
        "fr": (
            "Nous avons reçu la demande d’annulation ; aucune annulation "
            "ni aucun remboursement n’a encore eu lieu."
        ),
    },
    "modification_approved": {
        "ar": "وافق فريق العمليات على الطلب محليًا. لا يعني ذلك اكتمال تعديل Hostaway.",
        "en": "Operations approved the local request; Hostaway is not yet changed.",
        "fr": (
            "Les opérations ont approuvé la demande locale ; Hostaway n’a pas encore été modifié."
        ),
    },
    "modification_rejected": {
        "ar": "تعذر قبول طلب التعديل. بقي الحجز دون تغيير.",
        "en": "The modification request was declined. The reservation is unchanged.",
        "fr": "La demande de modification a été refusée. La réservation reste inchangée.",
    },
    "modification_price_changed": {
        "ar": "تغير فرق السعر أثناء إعادة التحقق ويحتاج إلى مراجعة جديدة.",
        "en": "The price difference changed and requires another review.",
        "fr": "La différence de prix a changé et nécessite un nouvel examen.",
    },
    "payment_succeeded": {
        "ar": "تم تسجيل نجاح الدفع. لا يُستخدم هذا القالب إلا بعد إثبات نجاح الدفع.",
        "en": "Payment succeeded. This template is only used after verified payment.",
        "fr": "Le paiement a réussi. Ce modèle n’est utilisé qu’après vérification du paiement.",
    },
    "reservation_confirmed": {
        "ar": "تم تأكيد الحجز بعد استلام معرف حجز Hostaway.",
        "en": "The reservation was confirmed after receiving a Hostaway reservation ID.",
        "fr": "La réservation a été confirmée après réception de l’identifiant Hostaway.",
    },
    "reservation_creation_failed": {
        "ar": "تعذر إنشاء الحجز ويحتاج فريق العمليات إلى المراجعة.",
        "en": "Reservation creation failed and requires operations review.",
        "fr": "La création de la réservation a échoué et nécessite l’examen des opérations.",
    },
    "reservation_unknown": {
        "ar": "حالة الحجز غير مؤكدة حاليًا وتخضع للمراجعة. لا تعاود الإجراء.",
        "en": "Reservation status is uncertain and under review. Do not retry.",
        "fr": "Le statut de la réservation est incertain et en cours d’examen. Ne réessayez pas.",
    },
    "reservation_cancelled": {
        "ar": "تم إلغاء الحجز بعد التحقق من الحالة النهائية.",
        "en": "The reservation was cancelled after final-state verification.",
        "fr": "La réservation a été annulée après vérification de son état final.",
    },
    "reservation_modified": {
        "ar": "تم تعديل الحجز بعد التحقق من الحالة النهائية.",
        "en": "The reservation was updated after final-state verification.",
        "fr": "La réservation a été mise à jour après vérification de son état final.",
    },
    "account_verify_email": {
        "ar": "تأكيد بريدك يحمي حسابك ويتيح لنا إرسال تفاصيل إقامتك إليك.",
        "en": "Confirming your address protects your account and lets us send your stay details.",
        "fr": (
            "Confirmer votre adresse protège votre compte et nous permet de vous "
            "envoyer les détails de votre séjour."
        ),
    },
    "account_password_reset": {
        "ar": "وصلنا طلب لإعادة تعيين كلمة مرور حسابك.",
        "en": "We received a request to reset the password for your account.",
        "fr": "Nous avons reçu une demande de réinitialisation du mot de passe de votre compte.",
    },
    "account_welcome": {
        "ar": "حسابك جاهز. تابع حجوزاتك وعدّل تواريخك من مكان واحد.",
        "en": "Your account is ready. Follow your bookings and change your dates in one place.",
        "fr": (
            "Votre compte est prêt. Suivez vos réservations et modifiez vos dates "
            "au même endroit."
        ),
    },
    "daily_operations_summary": {
        "ar": "يتوفر ملخص العمليات اليومي في لوحة الإدارة دون بيانات شخصية.",
        "en": "The PII-free daily operations summary is available in the admin dashboard.",
        "fr": "Le résumé quotidien sans données personnelles est disponible dans l’administration.",
    },
}


def recipient_hmac(email: str) -> str:
    normalized = email.strip().casefold()
    return salted_hmac("email-recipient.v1", normalized).hexdigest()


def _provider_name() -> str:
    if not settings.EMAIL_DELIVERY_ENABLED:
        return DisabledEmailProvider.name
    return DjangoEmailProvider.name


def _dispatch_email_delivery(delivery_id: object) -> None:
    """Dispatch immediately; the periodic queue remains the recovery path."""
    from apps.notifications.tasks import send_email_delivery_task

    send_email_delivery_task.delay(str(delivery_id))


def queue_email(
    *,
    message_type: str,
    recipient: str,
    recipient_source: str,
    recipient_reference: str,
    language: str,
    idempotency_key: str,
) -> EmailDelivery:
    normalized_language = language if language in {"ar", "en", "fr"} else "ar"
    subject = strip_tags(SUBJECTS[message_type][normalized_language]).strip()
    status = (
        EmailDelivery.Status.QUEUED
        if settings.EMAIL_DELIVERY_ENABLED
        else EmailDelivery.Status.DISABLED
    )
    delivery, created = EmailDelivery.objects.get_or_create(
        idempotency_key=idempotency_key,
        defaults={
            "message_type": message_type,
            "recipient_hash": recipient_hmac(recipient),
            "recipient_masked": mask_email(recipient),
            "recipient_source": recipient_source,
            "recipient_reference": str(recipient_reference),
            "language": normalized_language,
            "subject": subject,
            "template_name": TEMPLATE_GROUPS[message_type],
            "status": status,
            "provider": _provider_name(),
            "queued_at": timezone.now(),
            "last_error_code": (
                "" if settings.EMAIL_DELIVERY_ENABLED else "email_delivery_disabled"
            ),
        },
    )
    if created and delivery.status == EmailDelivery.Status.QUEUED:
        transaction.on_commit(
            lambda: _dispatch_email_delivery(delivery.pk),
            robust=True,
        )
    return delivery


def _account_links(delivery: EmailDelivery) -> tuple[str, dict[str, Any]]:
    """Resolve an account recipient and mint its link at send time.

    The token is generated here rather than when the row is queued, so its
    lifetime starts when the message actually leaves. A queue that backs up
    therefore delays the email instead of delivering a link that is already
    half expired.
    """
    from django.contrib.auth import get_user_model
    from django.contrib.auth.tokens import default_token_generator
    from django.utils.encoding import force_bytes
    from django.utils.http import urlsafe_base64_encode

    from apps.accounts.tokens import make_verification_token

    user = get_user_model().objects.filter(pk=int(delivery.recipient_reference)).first()
    if user is None or not user.email:
        raise EmailProviderError("recipient_not_available", permanent=True)

    base = settings.SITE_BASE_URL.rstrip("/")
    context: dict[str, Any] = {
        "reference": "",
        "first_name": user.first_name,
        "dashboard_url": f"{base}/my-bookings/",
    }
    if delivery.message_type == "account_verify_email":
        token = make_verification_token(user.pk, user.email)
        context["action_url"] = f"{base}/account/verify/{token}/"
        context["expires_in_days"] = 3
    elif delivery.message_type == "account_password_reset":
        uidb64 = urlsafe_base64_encode(force_bytes(user.pk))
        token = default_token_generator.make_token(user)
        context["action_url"] = f"{base}/account/reset/{uidb64}/{token}/"
        context["expires_in_hours"] = int(settings.PASSWORD_RESET_TIMEOUT / 3600)
    else:
        context["action_url"] = context["dashboard_url"]
    return user.email, context


def _resolve_recipient(delivery: EmailDelivery) -> tuple[str, dict[str, Any]]:
    if delivery.recipient_source == "account":
        return _account_links(delivery)
    if delivery.recipient_source == "contact":
        from apps.core.models import ContactMessage

        message = ContactMessage.objects.get(pk=int(delivery.recipient_reference))
        return message.email, {"reference": str(message.pk)}
    if delivery.recipient_source == "booking_intent":
        from apps.reservations.models import BookingIntent

        intent = BookingIntent.objects.select_related("property").get(
            public_reference=delivery.recipient_reference
        )
        return intent.guest_email, {
            "reference": intent.public_reference,
            "property_name": _localized_property_name(intent.property, delivery.language),
            "check_in": intent.check_in.strftime("%d/%m/%Y"),
            "check_out": intent.check_out.strftime("%d/%m/%Y"),
            "guests": intent.guests,
            "total_price": intent.total_price,
            "currency": intent.currency,
        }
    if delivery.recipient_source == "modification":
        from apps.reservations.models import BookingModificationRequest

        modification = BookingModificationRequest.objects.select_related(
            "reservation__booking_intent",
            "reservation__property",
        ).get(public_reference=delivery.recipient_reference)
        intent = modification.reservation.booking_intent
        if intent is None:
            raise EmailProviderError("recipient_not_available", permanent=True)
        return intent.guest_email, {
            "reference": modification.reservation.public_reference,
            "request_reference": modification.public_reference,
            "booking_reference": modification.reservation.public_reference,
            "property_name": (
                _localized_property_name(
                    modification.reservation.property,
                    delivery.language,
                )
                if modification.reservation.property
                else ""
            ),
            "check_in": (
                modification.new_check_in or modification.reservation.check_in
            ).strftime("%d/%m/%Y"),
            "check_out": (
                modification.new_check_out or modification.reservation.check_out
            ).strftime("%d/%m/%Y"),
            "guests": modification.new_guests or modification.reservation.guests,
            "total_price": (
                modification.new_total
                if modification.new_total is not None
                else modification.reservation.total_price
            ),
            "currency": modification.currency,
            "manage_url": f"{settings.SITE_BASE_URL}/reservations/manage/",
        }
    if delivery.recipient_source == "reservation":
        from apps.reservations.models import Reservation

        reservation = Reservation.objects.select_related(
            "booking_intent",
            "property",
        ).get(public_reference=delivery.recipient_reference)
        intent = reservation.booking_intent
        if intent is None:
            raise EmailProviderError("recipient_not_available", permanent=True)
        return intent.guest_email, {
            "reference": reservation.public_reference,
            "property_name": (
                _localized_property_name(reservation.property, delivery.language)
                if reservation.property
                else ""
            ),
            "manage_url": f"{settings.SITE_BASE_URL}/reservations/manage/",
            "check_in": reservation.check_in.strftime("%d/%m/%Y"),
            "check_out": reservation.check_out.strftime("%d/%m/%Y"),
            "guests": reservation.guests,
            "total_price": reservation.total_price,
            "currency": reservation.currency,
        }
    if delivery.recipient_source == "operations":
        if not settings.OPERATIONS_EMAIL:
            raise EmailProviderError("operations_email_not_configured", permanent=True)
        return settings.OPERATIONS_EMAIL, {}
    if delivery.recipient_source == "support":
        if not settings.SUPPORT_EMAIL:
            raise EmailProviderError("support_email_not_configured", permanent=True)
        return settings.SUPPORT_EMAIL, {}
    raise EmailProviderError("unsupported_recipient_source", permanent=True)


def _localized_property_name(property_obj: object, language: str) -> str:
    order = {
        "ar": ("name_ar", "name_en", "hostaway_name", "name_fr"),
        "en": ("name_en", "hostaway_name", "name_fr", "name_ar"),
        "fr": ("name_fr", "name_en", "hostaway_name", "name_ar"),
    }.get(language, ("name_ar", "name_en", "hostaway_name", "name_fr"))
    for field_name in order:
        value = getattr(property_obj, field_name, "")
        if value:
            return str(value)
    return ""


def send_queued_email(
    delivery_id: object,
    *,
    provider: EmailProvider | None = None,
) -> EmailSendResult:
    if not settings.EMAIL_DELIVERY_ENABLED:
        EmailDelivery.objects.filter(pk=delivery_id).exclude(
            status=EmailDelivery.Status.SENT
        ).update(
            status=EmailDelivery.Status.DISABLED,
            last_error_code="email_delivery_disabled",
        )
        return EmailSendResult(False, "email_delivery_disabled")

    with transaction.atomic():
        delivery = EmailDelivery.objects.select_for_update().get(pk=delivery_id)
        if delivery.status == EmailDelivery.Status.SENT:
            return EmailSendResult(False, "already_sent", delivery.provider_message_id)
        if delivery.status == EmailDelivery.Status.SENDING:
            return EmailSendResult(False, "already_sending")
        if delivery.status in {
            EmailDelivery.Status.CANCELLED,
            EmailDelivery.Status.SKIPPED,
            EmailDelivery.Status.DISABLED,
        }:
            return EmailSendResult(False, "not_sendable")
        if delivery.attempt_count >= settings.EMAIL_MAX_RETRIES:
            return EmailSendResult(False, "retry_limit_reached")
        delivery.status = EmailDelivery.Status.SENDING
        delivery.attempt_count += 1
        delivery.save(update_fields=["status", "attempt_count", "updated_at"])

    try:
        recipient, context = _resolve_recipient(delivery)
        if recipient_hmac(recipient) != delivery.recipient_hash:
            raise EmailProviderError("recipient_integrity_failed", permanent=True)
        request = EmailMessageRequest(
            recipient=recipient,
            subject=delivery.subject,
            template_name=delivery.template_name,
            language=delivery.language,
            context={
                **context,
                "heading": delivery.subject,
                "message": MESSAGES[delivery.message_type][delivery.language],
            },
        )
        result = (provider or DjangoEmailProvider()).send(request)
    except EmailProviderError as exc:
        now = timezone.now()
        EmailDelivery.objects.filter(pk=delivery.pk).update(
            status=EmailDelivery.Status.FAILED,
            last_error_code=("permanent_" if exc.permanent else "transient_") + exc.code,
            failed_at=now,
            updated_at=now,
        )
        logger.warning("Email delivery failed code=%s delivery=%s", exc.code, delivery.pk)
        return EmailSendResult(False, exc.code)

    now = timezone.now()
    EmailDelivery.objects.filter(pk=delivery.pk).update(
        status=EmailDelivery.Status.SENT,
        provider_message_id=result.provider_message_id,
        sent_at=now,
        failed_at=None,
        last_error_code="",
        updated_at=now,
    )
    return result
