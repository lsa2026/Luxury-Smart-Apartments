"""Verified-phone gifts and explicitly requested invitations, never an automatic campaign."""

from datetime import datetime, timedelta
from hashlib import sha256
from urllib.parse import urlparse

from celery import shared_task
from django.conf import settings
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.utils import timezone
from django.utils.crypto import salted_hmac
from django.utils.html import strip_tags

from apps.accounts.models import CustomerProfile, VerifiedPhone
from apps.accounts.phone_auth import _limit
from apps.accounts.phone_messages import supported_language
from apps.core.phone_numbers import normalize_phone_number
from apps.notifications.services.ultramsg import UltraMsgClient
from apps.payments.payment_link_tasks import PAYMENT_MESSAGE_BRAND

from .invitation_design import TEMPLATE_VERSION, invitation_caption, invitation_image
from .invitation_links import invitation_path
from .launch import participant_allowed, pilot_only
from .models import Member
from .services import invitation_eligible, member_for, posting_transaction, receipt, transfer

PREFIX = "gift-invite:"
# Owner-approved invitation-count exception (2026-10-16). This is the existing
# owner's server-owned membership number, never a form/email/recipient match.
# Authentication, consent, eligibility and per-request idempotency still apply.
UNLIMITED_INVITATION_MEMBERS = frozenset({"LSA-M000001"})


def invitation_limits_exempt(member):
    return bool(member.pk and member.number in UNLIMITED_INVITATION_MEMBERS)


def phone_hash(phone):
    return salted_hmac("loyalty-gift-phone-v1", phone, algorithm="sha256").hexdigest()


def recipient_for_phone(phone):
    binding = VerifiedPhone.objects.select_related("user").filter(phone=phone).first()
    if not binding or not binding.user.is_active or not participant_allowed(binding.user):
        return None, None
    member = (
        Member.objects.select_related("customer")
        .filter(customer=binding.user, status="active")
        .first()
    )
    return (member, binding) if member else (None, binding)


def recipient_language(phone):
    # A mutable contact field is never evidence of account ownership.
    binding = VerifiedPhone.objects.select_related("user").filter(phone=phone).first()
    if not binding or not participant_allowed(binding.user):
        return ""
    return supported_language(
        CustomerProfile.objects.filter(user_id=binding.user_id)
        .values_list("preferred_language", flat=True)
        .first()
    )


def masked_name(member):
    name = " ".join(strip_tags(member.customer.first_name).split())
    return name[:2] + "…" if name else "•••"


def confirm_phone_transfer(user, payload):
    with posting_transaction():
        proof = payload.get("phone_binding")
        if proof:
            binding = (
                VerifiedPhone.objects.select_related("user")
                .filter(pk=proof["id"], version=proof["version"])
                .first()
            )
            if not binding or phone_hash(binding.phone) != proof["phone_hash"]:
                raise ValidationError("Phone ownership changed; review the gift again.")
            member, current = recipient_for_phone(binding.phone)
            if not member or member.number != payload["recipient"] or current.pk != binding.pk:
                raise ValidationError("Recipient is no longer eligible.")
        return transfer(user, payload["request"], payload["recipient"], payload["value"])


def delivery_ready():
    # Independent of automatic loyalty notices and the sign-in pilot switches.
    return bool(
        settings.ULTRAMSG_ENABLED and settings.ULTRAMSG_INSTANCE_ID and settings.ULTRAMSG_TOKEN
    )


def _enabled(user):
    from .views import approved_terms

    return (
        settings.LOYALTY_ENABLED
        and settings.LOYALTY_UI_ENABLED
        and approved_terms()
        and user.is_active
        and participant_allowed(user)
        and CustomerProfile.objects.filter(user=user, email_verified_at__isnull=False).exists()
    )


def queue_invitation(user, *, phone, language, key, consent):
    if not _enabled(user) or consent is not True:
        raise PermissionDenied
    phone = normalize_phone_number(phone)
    language = recipient_language(phone) or supported_language(language)
    if not language or not key.startswith(PREFIX) or len(key) > 100 or not delivery_ready():
        raise ValidationError("Invitation unavailable.")
    with posting_transaction():
        sender = member_for(user)
        if not sender.invitation_token or not invitation_eligible(sender):
            raise ValidationError("Invitation not eligible.")
        recipient, _ = recipient_for_phone(phone)
        if recipient:
            raise ValidationError("Review the recipient again.")

        def work():
            unlimited = invitation_limits_exempt(sender)
            if not unlimited and not (
                _limit("gift-invite-sender", sender.pk, 5, 86400)
                and _limit("gift-invite-target", phone, 1, 86400)
                and _limit("gift-invite-global", "site", 50, 86400)
            ):
                raise ValidationError("Invitation request limit.")
            result = {
                "phone": phone,
                "language": language,
                "status": "queued",
                "requested_at": timezone.now().isoformat(),
                "consent": True,
                "template_version": TEMPLATE_VERSION,
                "limit_policy": "owner-unlimited" if unlimited else "standard",
            }
            transaction.on_commit(lambda: _dispatch(sender.pk, key), robust=True)
            return result

        receipt(sender, key, ["gift-invitation", phone_hash(phone), language], work)
        return {"member": sender.pk, "key": key}


def _dispatch(member_id, key):
    try:
        deliver_invitation.delay(member_id, key)
    except Exception:
        _finish(member_id, key, "queue_failed", expected="queued")


def _finish(member_id, key, status, *, expected="sending", provider_reference=""):
    with posting_transaction():
        member = Member.objects.select_for_update().get(pk=member_id)
        record = member.command_receipts.get(key, {}).get("result", {})
        if record.get("status") == expected:
            record.update(status=status, updated_at=timezone.now().isoformat())
            if provider_reference:
                record["provider_reference"] = provider_reference
            member.save(update_fields=["command_receipts", "updated_at"])


def invitation_status(user, pointer):
    member = Member.objects.filter(customer=user).first()
    if not member or not isinstance(pointer, dict) or pointer.get("member") != member.pk:
        return None
    row = member.command_receipts.get(str(pointer.get("key", "")), {}).get("result", {})
    if "phone" not in row or "status" not in row:
        return None
    status = row["status"]
    if status in {"queued", "sending"} and timezone.now() - datetime.fromisoformat(
        row["requested_at"]
    ) > timedelta(minutes=15):
        status = "uncertain"
    return {"phone": "••••" + row["phone"][-4:], "status": status}


def _sender_name(sender, language):
    # A guest-controlled name must not inject links or WhatsApp formatting.
    name = " ".join(
        "".join(
            c for c in strip_tags(sender.customer.first_name) if c.isalpha() or c.isspace()
        ).split()
    )[:60]
    return (
        name or {"ar": "أحد ضيوفنا", "en": "one of our guests", "fr": "un de nos clients"}[language]
    )


def invitation_message(sender, *, language, url):
    return invitation_caption(_sender_name(sender, language), language=language, url=url)


def legacy_invitation_message(sender, *, language, url):
    """Keep already queued version-one requests in their original text format."""
    name = _sender_name(sender, language)
    title, intro, steps, optional = {
        "ar": (
            "🎁 *دعوة إلى مكافآت ضيوفنا*",
            f"بدعوة من {name}، يسعدنا أن تنضم إلى برنامج الولاء لدينا.",
            "أنشئ حسابك أو سجّل الدخول، ثم انضم باختيارك ووثّق جوالك لتتمكن من استلام هدايا النقاط.",
            "الانضمام مجاني واختياري. هذه دعوة فقط؛ لم تُحوّل أو تُحجز لك نقاط. "
            "إذا لم تكن تتوقعها، يمكنك تجاهلها.",
        ),
        "en": (
            "🎁 *An invitation to guest rewards*",
            f"{name} has invited you to our rewards programme.",
            "Create an account or sign in, choose to join and verify your mobile "
            "to receive gifts of points.",
            "Joining is free and optional. This is an invitation only; "
            "no points have been sent or reserved. Ignore it if unexpected.",
        ),
        "fr": (
            "🎁 *Une invitation à nos avantages*",
            f"{name} vous invite à notre programme de fidélité.",
            "Créez un compte ou connectez-vous, choisissez d’adhérer et vérifiez votre mobile "
            "pour recevoir des points en cadeau.",
            "L’adhésion est gratuite et facultative. Aucun point n’a été transféré ni réservé. "
            "Ignorez ce message si vous ne l’attendiez pas.",
        ),
    }[language]
    if pilot_only():
        optional += (
            "\n"
            + {
                "ar": "البرنامج حاليًا تجربة خاصة، والانضمام متاح للحسابات المعتمدة للتجربة فقط.",
                "en": "The programme is currently a private trial for approved accounts only.",
                "fr": "Le programme est actuellement un essai privé réservé aux comptes approuvés.",
            }[language]
        )
    return "\n\n".join([f"✨ *{PAYMENT_MESSAGE_BRAND}*", title, intro, steps, url, optional])


@shared_task(ignore_result=True, soft_time_limit=35, time_limit=45)
def deliver_invitation(member_id, key):
    # Claim once before network I/O; an unknown outcome must never be resent automatically.
    with posting_transaction():
        sender = Member.objects.select_for_update().select_related("customer").get(pk=member_id)
        row = sender.command_receipts.get(key, {}).get("result", {})
        if row.get("status") != "queued":
            return
        if (
            not _enabled(sender.customer)
            or sender.status != "active"
            or not sender.invitation_token
            or not invitation_eligible(sender)
            or not delivery_ready()
            or timezone.now() - datetime.fromisoformat(row["requested_at"]) > timedelta(minutes=15)
        ):
            row["status"] = "disabled"
            sender.save(update_fields=["command_receipts", "updated_at"])
            return
        recipient, _ = recipient_for_phone(row["phone"])
        if recipient:
            row["status"] = "recipient_ready"
            sender.save(update_fields=["command_receipts", "updated_at"])
            return
        phone, language = row["phone"], recipient_language(row["phone"]) or row["language"]
        base = settings.SITE_BASE_URL.rstrip("/")
        url = base + invitation_path(sender)
        version = row.get("template_version", "gift-whatsapp-20261016-v1")
        try:
            if (
                urlparse(base).scheme != "https"
                or not urlparse(base).netloc
                or version not in {TEMPLATE_VERSION, "gift-whatsapp-20261016-v1"}
            ):
                raise ValidationError("Invitation design unavailable.")
            if version == TEMPLATE_VERSION:
                content = invitation_image(language)
                body = invitation_message(sender, language=language, url=url)
                row["media_sha256"] = sha256(content).hexdigest()
            else:
                content = None
                body = legacy_invitation_message(sender, language=language, url=url)
        except (OSError, ValidationError, KeyError):
            row["status"] = "disabled"
            sender.save(update_fields=["command_receipts", "updated_at"])
            return
        row.update(status="sending", delivery_language=language)
        sender.save(update_fields=["command_receipts", "updated_at"])
    try:
        with UltraMsgClient() as client:
            reference_id = f"lsa-gift-{phone_hash(str(member_id) + key)[:32]}"
            if content is not None:
                response = client.send_image(
                    recipient=phone, content=content, caption=body, reference_id=reference_id
                )
            else:
                response = client.send_text(recipient=phone, body=body, reference_id=reference_id)
    except Exception:
        _finish(member_id, key, "uncertain")
        return
    reference = str(response.get("id", ""))[:80] if isinstance(response, dict) else ""
    reference = "".join(c for c in reference if c.isalnum() or c in "-_")
    _finish(member_id, key, "accepted", provider_reference=reference)
