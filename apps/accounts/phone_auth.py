"""Guest phone credential: explicit primary-auth binding, then one-use WhatsApp OTP."""

import logging
import secrets
from datetime import timedelta

from celery import shared_task
from django.conf import settings
from django.db import IntegrityError, transaction
from django.utils import timezone
from django.utils.crypto import constant_time_compare, salted_hmac

from apps.core.phone_numbers import normalize_phone_number
from apps.notifications.services.ultramsg import UltraMsgClient, UltraMsgError
from apps.reservations.security import session_key_hash

from .models import (
    CustomerProfile,
    OperationsManager,
    PhoneAuthLimit,
    PhoneChallenge,
    VerifiedPhone,
)
from .phone_messages import phone_message, supported_language

PRIMARY_AUTH = "guest_primary_auth_at"
PENDING = "guest_phone_challenge"
TTL = 300
logger = logging.getLogger(__name__)


def _provider_authenticated(document):
    """Read the provider's nested account status, never a truthy object/string."""
    if not isinstance(document, dict):
        return False
    state = document.get("status")
    if isinstance(state, dict):
        state = state.get("accountStatus")
    if isinstance(state, dict):
        state = state.get("status")
    return isinstance(state, str) and state == "authenticated"


def ready():
    return bool(
        settings.ACCOUNT_WHATSAPP_AUTH_ENABLED
        and settings.ACCOUNT_WHATSAPP_AUTH_ISOLATED
        and (settings.ACCOUNT_WHATSAPP_AUTH_PUBLIC or settings.ACCOUNT_WHATSAPP_AUTH_EMAILS)
        and settings.ULTRAMSG_ENABLED
        and settings.ULTRAMSG_INSTANCE_ID
        and settings.ULTRAMSG_TOKEN
    )


def allowed(user):
    if not user or not user.is_authenticated or not user.is_active:
        return False
    email = (user.email or "").strip().casefold()
    return bool(
        email
        and (
            settings.ACCOUNT_WHATSAPP_AUTH_PUBLIC
            or email in {str(v).strip().casefold() for v in settings.ACCOUNT_WHATSAPP_AUTH_EMAILS}
        )
        and email != settings.OPERATIONS_OWNER_EMAIL.strip().casefold()
        and not user.is_staff
        and not user.is_superuser
        and not OperationsManager.objects.filter(email__iexact=email, is_enabled=True).exists()
        and CustomerProfile.objects.filter(user=user, email_verified_at__isnull=False).exists()
    )


def recent_primary(request):
    value = request.session.get(PRIMARY_AUTH)
    return bool(
        isinstance(value, dict)
        and value.get("user") == request.user.pk
        and isinstance(value.get("at"), int)
        and 0 <= int(timezone.now().timestamp()) - value["at"] <= 900
    )


def _digest(scope, value):
    return salted_hmac(f"accounts.whatsapp.{scope}.v1", str(value), algorithm="sha256").hexdigest()


def _limit(scope, value, maximum, seconds):
    now = timezone.now()
    bucket = int(now.timestamp()) // seconds
    key = _digest("limit", f"{scope}:{value}:{bucket}")
    # get_or_create's unique key resolves creation races; the row lock serializes
    # all increments across site processes. No local-cache fallback on failure.
    with transaction.atomic():
        PhoneAuthLimit.objects.get_or_create(
            key=key, defaults={"expires_at": now + timedelta(seconds=seconds * 2)}
        )
        row = PhoneAuthLimit.objects.select_for_update().get(pk=key)
        row.count += 1
        row.save(update_fields=["count"])
        return row.count <= maximum


def _dispatch(challenge_id):
    try:
        deliver_phone_code.delay(challenge_id)
    except Exception:
        # Queue outage is not permission to send inline or disclose broker URLs.
        PhoneChallenge.objects.filter(pk=challenge_id, status="pending").update(status="failed")


def _cooldown(phone):
    now = timezone.now()
    key = _digest("cooldown", phone)
    with transaction.atomic():
        PhoneAuthLimit.objects.get_or_create(key=key, defaults={"expires_at": now})
        row = PhoneAuthLimit.objects.select_for_update().get(pk=key)
        if row.count and row.expires_at > now:
            return False
        row.count += 1
        row.expires_at = now + timedelta(seconds=60)
        row.save(update_fields=["count", "expires_at"])
        return True


def begin(request, phone, *, purpose):
    if not ready() or purpose not in {"bind", "login"}:
        return None
    phone = normalize_phone_number(phone)
    binding = VerifiedPhone.objects.select_related("user").filter(phone=phone).first()
    user = binding.user if binding else None
    if purpose == "bind":
        user = request.user
        # A recent verified primary sign-in proves the account; consuming the
        # WhatsApp code proves possession of the new number. A booking/contact
        # match is neither required nor sufficient, and no stays are claimed.
        if (
            not allowed(user)
            or not recent_primary(request)
            or VerifiedPhone.objects.filter(user=user).exists()
            or binding is not None
        ):
            return None
    elif request.user.is_authenticated:
        return None
    # Stop before allocating per-number rows once the shared caps are reached.
    if (
        not _limit("global", "site", 100, 900)
        or not _limit("ip", request.META.get("REMOTE_ADDR", ""), 10, 900)
        or not _limit("phone", phone, 3, 900)
        or not _cooldown(phone)
    ):
        return None
    previous = request.session.get(PENDING)
    digest = session_key_hash(request)
    with transaction.atomic():
        if previous:
            PhoneChallenge.objects.filter(
                pk=previous, session_digest=digest, status__in=["pending", "sending", "sent"]
            ).update(status="superseded", code_digest="")
        challenge = PhoneChallenge.objects.create(
            user=user if allowed(user) else None,
            phone=phone,
            purpose=purpose,
            language=(
                supported_language(
                    CustomerProfile.objects.filter(user=user)
                    .values_list("preferred_language", flat=True)
                    .first()
                )
                if user is not None
                else ""
            )
            or supported_language(getattr(request, "LANGUAGE_CODE", ""))
            or "ar",
            binding_version=binding.version if binding else None,
            session_digest=digest,
            expires_at=timezone.now() + timedelta(seconds=TTL),
        )
        request.session[PENDING] = str(challenge.pk)
        # The broker receives a random identifier only, never phone/code/body.
        transaction.on_commit(lambda: _dispatch(str(challenge.pk)))
    return challenge


def _current_binding(row):
    return VerifiedPhone.objects.filter(
        user_id=row.user_id, phone=row.phone, version=row.binding_version
    ).exists()


@shared_task(ignore_result=True, soft_time_limit=35, time_limit=40)
def deliver_phone_code(challenge_id):
    """No retries or result/log payloads containing OTPs, phone numbers or responses."""
    if not ready():
        return
    with transaction.atomic():
        row = (
            PhoneChallenge.objects.select_for_update(of=("self",))
            .select_related("user")
            .get(pk=challenge_id)
        )
        if row.status != "pending" or row.expires_at <= timezone.now() or not allowed(row.user):
            return
        if row.purpose == "login" and not _current_binding(row):
            return
        code = f"{secrets.randbelow(1_000_000):06d}"
        row.code_digest = _digest("code", f"{row.pk}:{code}")
        row.status = "sending"
        row.save(update_fields=["code_digest", "status"])
    phase = "connection_check"
    try:
        with UltraMsgClient() as client:
            status = client.http.get(
                f"/{settings.ULTRAMSG_INSTANCE_ID}/instance/status",
                params={"token": settings.ULTRAMSG_TOKEN},
            )
            if status.status_code != 200 or not _provider_authenticated(status.json()):
                raise UltraMsgError()
            # Recheck immediately before the provider call: queued/late tasks
            # cannot extend validity. Acceptance is NOT proof of delivery.
            if not PhoneChallenge.objects.filter(
                pk=row.pk, status="sending", expires_at__gt=timezone.now()
            ).exists():
                return
            phase = "send"
            client.send_text(
                recipient=row.phone,
                body=phone_message(code=code, language=row.language, purpose=row.purpose),
                reference_id=f"lsa-auth-{row.pk}",
            )
    except Exception:
        # Provider exceptions may contain a credential-bearing request URL.
        # Never log/return them or the body, and never retry an unknown send.
        PhoneChallenge.objects.filter(pk=row.pk, status="sending").update(
            status="failed", code_digest=""
        )
        logger.warning("guest_whatsapp_delivery_failed stage=%s", phase)
        return
    PhoneChallenge.objects.filter(pk=row.pk, status="sending").update(status="sent")


def consume(request, submitted):
    if not ready():
        return None
    challenge_id = request.session.get(PENDING)
    if not challenge_id:
        return None
    if not _limit("verify-ip", request.META.get("REMOTE_ADDR", ""), 30, 900):
        return None
    with transaction.atomic():
        row = (
            PhoneChallenge.objects.select_for_update(of=("self",))
            .select_related("user")
            .filter(pk=challenge_id)
            .first()
        )
        if (
            not row
            or row.status != "sent"
            or row.expires_at <= timezone.now()
            or row.attempts >= 5
            or not constant_time_compare(row.session_digest, session_key_hash(request))
            or not allowed(row.user)
        ):
            return None
        row.attempts += 1
        valid = bool(
            isinstance(submitted, str)
            and len(submitted) == 6
            and submitted.isascii()
            and submitted.isdigit()
            and constant_time_compare(row.code_digest, _digest("code", f"{row.pk}:{submitted}"))
        )
        if not valid:
            if row.attempts >= 5:
                row.status, row.code_digest = "locked", ""
            row.save(update_fields=["attempts", "status", "code_digest"])
            return None
        if row.purpose == "bind":
            if request.user.pk != row.user_id or not recent_primary(request):
                return None
            try:
                with transaction.atomic():
                    VerifiedPhone.objects.create(user=row.user, phone=row.phone)
            except IntegrityError:
                return None
        elif request.user.is_authenticated or not _current_binding(row):
            return None
        row.status, row.code_digest = "consumed", ""
        row.save(update_fields=["attempts", "status", "code_digest"])
        request.session.pop(PENDING, None)
        return row
