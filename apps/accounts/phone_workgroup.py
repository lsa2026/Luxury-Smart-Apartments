"""One-use transient OTP copy to the fixed team; journals retain only hashes.

No code is generated at request allocation. A random generation nonce is fixed
at the first eligible task, then a keyed PRF permits source reconstruction for
the single private readback. Consuming that readback is permanent before its
response: a lost response is unknown, never permission to release/send again.
"""

import base64
import hashlib
import json
import uuid

from django.conf import settings
from django.db import transaction
from django.utils import timezone
from django.utils.crypto import salted_hmac
from django.utils.dateparse import parse_datetime
from django.views.decorators.debug import sensitive_variables

from apps.reservations import telegram_website_workgroup as transport
from apps.reservations import telegram_workgroup as group
from apps.reservations.telegram_authority import Denied
from apps.reservations.telegram_stay_models import TelegramWebsiteHandoff
from apps.reservations.telegram_stay_render import sha

from . import phone_auth as auth
from .models import CustomerProfile, PhoneChallenge, VerifiedPhone
from .phone_messages import phone_logo, phone_message


def admitted(created):
    try:
        cutover = parse_datetime(settings.ACCOUNT_WORKGROUP_AUTH_CUTOVER_AT)
        return bool(cutover and timezone.is_aware(cutover) and cutover <= created <= timezone.now())
    except (TypeError, ValueError):
        return False


def enabled():
    return bool(
        group.enabled() and settings.ACCOUNT_WORKGROUP_AUTH_ENABLED and admitted(timezone.now())
    )


def current(challenge):
    if (
        not auth.ready()
        or not admitted(challenge.created_at)
        or challenge.expires_at <= timezone.now()
        or challenge.purpose not in {"bind", "login"}
        or challenge.language not in {"ar", "en", "fr"}
        or not challenge.user_id
        or not auth.allowed(challenge.user)
    ):
        return False
    if challenge.purpose == "bind":
        from .identity import can_bind_phone

        return bool(
            CustomerProfile.objects.filter(
                user=challenge.user,
                email_verified_at__isnull=False,
            ).exists()
            and not VerifiedPhone.objects.filter(user=challenge.user).exists()
            and not VerifiedPhone.objects.filter(phone=challenge.phone).exists()
            and can_bind_phone(challenge.user, challenge.phone)
        )
    return auth._current_binding(challenge)


@sensitive_variables()
def code_for(challenge):
    if not challenge.workgroup_nonce:
        raise Denied
    value = ":".join(
        [
            str(challenge.pk),
            str(challenge.workgroup_nonce),
            challenge.session_digest,
            challenge.purpose,
            challenge.phone,
        ]
    )
    digest = salted_hmac("accounts.workgroup.otp.v1", value, algorithm="sha256").hexdigest()
    return f"{int(digest, 16) % 1_000_000:06d}"


@sensitive_variables()
def content_nonce_for(challenge):
    """Private hash blinding: never expose a six-digit brute-force oracle in DB."""
    if not challenge.workgroup_nonce:
        raise Denied
    return salted_hmac(
        "accounts.workgroup.content-nonce.v1",
        f"{challenge.pk}:{challenge.workgroup_nonce}",
        algorithm="sha256",
    ).hexdigest()


@sensitive_variables()
def packet(challenge):
    if challenge.status != "sending" or not current(challenge):
        raise Denied
    code = code_for(challenge)
    if challenge.code_digest != auth._digest("code", f"{challenge.pk}:{code}"):
        raise Denied
    image = phone_logo()
    if not image.startswith(b"\x89PNG\r\n\x1a\n") or len(image) > 300_000:
        raise Denied
    caption = transport.as_html(
        phone_message(
            code=code,
            language=challenge.language,
            purpose=challenge.purpose,
        )
    )
    asset_sha = hashlib.sha256(image).hexdigest()
    content_nonce = content_nonce_for(challenge)
    content_sha = sha(
        json.dumps(
            [challenge.language, "HTML", "photo", caption, asset_sha, content_nonce],
            ensure_ascii=False,
            separators=(",", ":"),
        )
    )
    basis = {
        "challenge": str(challenge.pk),
        "generation": str(challenge.workgroup_nonce),
        "session": challenge.session_digest,
        "purpose": challenge.purpose,
        "binding": str(challenge.binding_version or ""),
        "user": challenge.user_id,
        "code_digest": challenge.code_digest,
        "expires": challenge.expires_at.isoformat(),
    }
    return {
        "source_kind": "phone_code",
        "source_id": str(challenge.pk),
        "source_revision": transport.digest(basis),
        "phone_hash": sha(challenge.phone[1:]),
        "guest_phone_e164": challenge.phone,
        "language": challenge.language,
        "purpose": "phone_" + challenge.purpose,
        "content_sha256": content_sha,
        "content_nonce": content_nonce,
        "method": "photo",
        "caption_html": caption,
        "parse_mode": "HTML",
        "asset_sha256": asset_sha,
        "asset_mime": "image/png",
        "asset_base64": base64.b64encode(image).decode("ascii"),
        "not_before": challenge.created_at.isoformat(),
        "expires_at": challenge.expires_at.isoformat(),
    }


@sensitive_variables()
def deliver(challenge_id):
    if not enabled():
        return
    with transaction.atomic():
        challenge = (
            PhoneChallenge.objects.select_for_update(of=("self",))
            .select_related("user")
            .get(
                pk=challenge_id,
            )
        )
        if challenge.status != "pending" or challenge.workgroup_nonce or not current(challenge):
            return
        if TelegramWebsiteHandoff.objects.filter(job_key="site01otp:" + str(challenge.pk)).exists():
            return
        challenge.workgroup_nonce = uuid.uuid4()
        challenge.code_digest = auth._digest("code", f"{challenge.pk}:{code_for(challenge)}")
        challenge.status = "sending"
        challenge.save(update_fields=["workgroup_nonce", "code_digest", "status"])
        content = packet(challenge)
        key = "site01otp:" + str(challenge.pk)
        row, created = TelegramWebsiteHandoff.objects.get_or_create(
            job_key=key,
            defaults={
                "id": challenge.pk,
                "source_kind": "phone_code",
                "source_id": str(challenge.pk),
                "source_revision": content["source_revision"],
                "reference_id": "site01otp-" + str(challenge.pk),
                "phone_hash": content["phone_hash"],
                "content_sha256": content["content_sha256"],
                "asset_sha256": content["asset_sha256"],
                "state": "dispatching",
                "not_before": challenge.created_at,
                "expires_at": challenge.expires_at,
            },
        )
        if not created:
            return  # Never adopt or POST an older generation/journal.
    # Only hashes/IDs leave this frame; private authority reconstructs once.
    del content
    transport.call(row, "phone_code_handoff")


@sensitive_variables()
def authorize(scope, action, key, result):
    if not group.manual_mode() or action not in {"phone_code_handoff", "phone_code_handoff_status"}:
        raise Denied
    with transaction.atomic():
        row = TelegramWebsiteHandoff.objects.select_for_update().get(pk=uuid.UUID(scope["job"]))
        if key != str(row.pk) or row.source_kind != "phone_code":
            raise Denied
        value = {"contract": "tg39_phone_code_packet_v1", **transport.metadata(row)}
        if action == "phone_code_handoff":
            if not enabled() or row.state != "dispatching":
                raise Denied
            challenge = (
                PhoneChallenge.objects.select_for_update(of=("self",))
                .select_related("user")
                .get(
                    pk=row.source_id,
                )
            )
            if (
                challenge.workgroup_authorized_at
                or not row.not_before <= timezone.now() < row.expires_at
            ):
                raise Denied
            content = packet(challenge)
            if any(
                content[k] != getattr(row, k)
                for k in (
                    "source_revision",
                    "phone_hash",
                    "content_sha256",
                    "asset_sha256",
                )
            ):
                raise Denied
            challenge.workgroup_authorized_at = timezone.now()
            challenge.save(update_fields=["workgroup_authorized_at"])
            value.update(content)
        result.update(
            source_scope="phone_code_handoff",
            dispatch_mode="workgroup_manual",
            workgroup_id=group.WORKGROUP_ID,
            packet=value,
        )
    return result


def finish(source_id):
    # A group receipt is not verification; the original expiry/session still
    # governs consume. Never revive a superseded/locked/consumed challenge.
    PhoneChallenge.objects.filter(pk=source_id, status="sending").update(status="handed_off")


def expire_unknown(row):
    if row.source_kind == "phone_code" and row.expires_at <= timezone.now():
        PhoneChallenge.objects.filter(pk=row.source_id, status="sending").update(
            status="failed",
            code_digest="",
        )
