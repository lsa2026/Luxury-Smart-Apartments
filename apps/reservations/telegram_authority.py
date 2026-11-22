"""Short-lived, action-scoped readback. No reusable outbound credential at the site.

Capabilities are signed, not encrypted. The approved-phone claim travels only to
the same trusted bridge that receives that phone on readback, over HTTPS. Never
persist capabilities in sessions, database, analytics, exceptions or task logs.
"""

import json
import re
import uuid
from datetime import UTC, datetime
from functools import wraps

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core import signing
from django.db import transaction
from django.http import JsonResponse
from django.utils import timezone
from django.utils.crypto import constant_time_compare, salted_hmac
from django.views.decorators.debug import sensitive_variables

from apps.accounts.access import (
    MANAGER_SESSION_KEY,
    has_operations_access,
    is_operations_owner,
    normalize_email,
)
from apps.accounts.models import OperationsManager, VerifiedPhone
from apps.loyalty.invitation_auth import active_credential, authenticated
from apps.loyalty.models import InvitationAPICredential

from .models import Reservation, TelegramActivation

SALT = "telegram39.activation-authority.v1"
TTL = 120
ACTIONS = {"health", "prepare", "status", "binding", "approve_phone", "issue_invitation"}
WRITES = {"approve_phone": "approve_authorized_at", "issue_invitation": "issue_authorized_at"}


class Denied(Exception):
    pass


def phone_digest(phone):
    return salted_hmac("telegram-activation-phone.v1", phone).hexdigest()


def booking_scope(request, reservation, *, admin=False, row=None, phone=None):
    user = request.user
    if not user.is_authenticated or not user.is_active:
        raise Denied
    if admin:
        if not has_operations_access(request):
            raise Denied
        scope = {
            "kind": "operations",
            "grant": request.session.get(MANAGER_SESSION_KEY),
        }
    else:
        if not reservation.booking_intent_id or reservation.booking_intent.customer_id != user.pk:
            raise Denied
        scope = {"kind": "guest"}
    scope.update(
        actor=user.pk, reservation=str(reservation.pk), key=str(reservation.hostaway_reservation_id)
    )
    if row:
        if not phone or phone_digest(phone) != row.phone_digest or row.actor_id != user.pk:
            raise Denied
        scope.update(proof=str(row.proof_id), phone=phone)
        scope["evidence_ref"] = f"{row.proof_kind}:{row.proof_id}"
        if not admin:
            binding = VerifiedPhone.objects.filter(user=user, phone=phone).first()
            if not binding:
                raise Denied
            scope["binding_version"] = str(binding.version)
    return scope


def loyalty_scope(key):
    credential = active_credential.get()
    if not credential or not getattr(settings, "TELEGRAM_LOYALTY_INVITATIONS_ENABLED", False):
        raise Denied
    return {"kind": "loyalty", "credential": credential, "key": key}


@sensitive_variables()
def mint(scope, action, key=None):
    if not isinstance(scope, dict) or action not in ACTIONS:
        raise Denied
    now = int(timezone.now().timestamp())
    request_id = uuid.uuid4()
    value = {
        "scope": dict(scope),
        "action": action,
        "key": key,
        "issued": now,
        "expires": now + TTL,
    }
    if action in WRITES:
        if not scope.get("proof") or not scope.get("phone"):
            raise Denied
        request_id = uuid.uuid5(uuid.UUID(scope["proof"]), action)
    else:
        # No phone is needed or released for any read-only operation.
        value["scope"].pop("phone", None)
    value["request_id"] = str(request_id)
    token = signing.dumps(value, salt=SALT, compress=True)
    if len(token) > 2048:
        raise Denied
    return {
        "contract": "tg39_activation_bridge_v1",
        "request_id": str(request_id),
        "action": action,
        "capability": token,
    }


def duplicate_safe(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise Denied
        result[key] = value
    return result


def actor_allowed(scope, reservation):
    actor = get_user_model().objects.filter(pk=scope.get("actor"), is_active=True).first()
    if not actor:
        raise Denied
    if scope.get("kind") == "guest":
        if not reservation.booking_intent_id or reservation.booking_intent.customer_id != actor.pk:
            raise Denied
    elif scope.get("kind") == "operations":
        if not is_operations_owner(actor):
            grant = scope.get("grant")
            if (
                not isinstance(grant, dict)
                or grant.get("user_id") != actor.pk
                or grant.get("email") != normalize_email(actor.email)
                or not OperationsManager.objects.filter(
                    pk=grant.get("grant_id"),
                    email=normalize_email(actor.email),
                    access_version=grant.get("version"),
                    is_enabled=True,
                ).exists()
            ):
                raise Denied
    else:
        raise Denied
    return actor


@sensitive_variables()
def authorize(data):
    if (
        not isinstance(data, dict)
        or set(data) != {"contract", "request_id", "action", "capability"}
        or data.get("contract") != "tg39_activation_readback_v1"
        or data.get("action") not in ACTIONS
        or not isinstance(data.get("request_id"), str)
        or str(uuid.UUID(data["request_id"])) != data["request_id"]
        or not isinstance(data.get("capability"), str)
        or not 1 <= len(data["capability"]) <= 2048
    ):
        raise Denied
    claims = signing.loads(data["capability"], salt=SALT, max_age=TTL)
    now = int(timezone.now().timestamp())
    if (
        not isinstance(claims, dict)
        or claims.get("request_id") != data["request_id"]
        or claims.get("action") != data["action"]
        or type(claims.get("issued")) is not int
        or type(claims.get("expires")) is not int
        or not claims["issued"] <= now < claims["expires"] <= claims["issued"] + TTL
    ):
        raise Denied
    scope, action, key = claims["scope"], data["action"], claims["key"]
    if key is not None and (not isinstance(key, str) or not re.fullmatch(r"[1-9][0-9]{1,15}", key)):
        raise Denied
    result = {
        "contract": "tg39_activation_authority_v1",
        "authorized": True,
        "request_id": data["request_id"],
        "action": action,
        "reservation_key": key,
        "expires_at": datetime.fromtimestamp(claims["expires"], UTC).isoformat(),
    }
    if scope.get("kind") == "loyalty":
        if (
            action not in {"health", "binding"}
            or not getattr(settings, "TELEGRAM_LOYALTY_INVITATIONS_ENABLED", False)
            or scope.get("key") != key
            or scope.get("credential") != active_credential.get()
            or not InvitationAPICredential.objects.filter(
                pk=scope["credential"], revoked_at__isnull=True, expires_at__gt=timezone.now()
            ).exists()
        ):
            raise Denied
        return result
    if not getattr(settings, "TELEGRAM_ACTIVATION_ENABLED", False):
        raise Denied
    with transaction.atomic():
        reservation = (
            Reservation.objects.select_for_update(of=("self",))
            .select_related("booking_intent")
            .get(pk=scope["reservation"])
        )
        from .telegram_activation import eligible, key_for

        actor = actor_allowed(scope, reservation)
        if not eligible(reservation) or key_for(reservation) != key:
            raise Denied
        if action == "binding":
            raise Denied  # Binding lookup belongs to the existing invitation API only.
        if action in WRITES:
            row = TelegramActivation.objects.select_for_update().get(reservation=reservation)
            field = WRITES[action]
            if (
                row.state != "processing"
                or str(row.proof_id) != scope.get("proof")
                or row.actor_id != actor.pk
                or getattr(row, field) is not None
                or str(uuid.uuid5(row.proof_id, action)) != data["request_id"]
                or not constant_time_compare(row.phone_digest, phone_digest(scope.get("phone", "")))
                or not re.fullmatch(r"\+[1-9][0-9]{6,14}", scope.get("phone", ""))
            ):
                raise Denied
            if scope["kind"] == "guest":
                if (
                    row.proof_kind != "website_verified"
                    or reservation.booking_intent.guest_phone != scope["phone"]
                    or not VerifiedPhone.objects.filter(
                        user=actor, phone=scope["phone"], version=scope.get("binding_version")
                    ).exists()
                ):
                    raise Denied
            elif row.proof_kind != "admin_verified":
                raise Denied
            if action == "issue_invitation" and row.approve_authorized_at is None:
                raise Denied
            setattr(row, field, timezone.now())
            row.save(update_fields=[field])  # Durable consume before returning authority/RPC.
            if action == "approve_phone":
                result.update(
                    phone_proof_verified=True,
                    phone=scope["phone"][1:],
                    evidence_ref=f"{row.proof_kind}:{row.proof_id}",
                )
    return result


def private_response(value, status=200):
    result = JsonResponse(value, status=status)
    return seal(result)


def seal(result):
    result["Cache-Control"] = "no-store, private, max-age=0"
    result["Referrer-Policy"] = "no-referrer"
    result["X-Robots-Tag"] = "noindex, nofollow"
    result["Content-Security-Policy"] = "default-src 'none'; frame-ancestors 'none'"
    return result


def private_endpoint(view):
    @wraps(view)
    def wrapped(request, *args, **kwargs):
        request._disable_google_integrations = True
        return seal(view(request, *args, **kwargs))

    return wrapped


@private_endpoint
@authenticated("POST")
@sensitive_variables()
def authority(request):
    request._disable_google_integrations = True
    try:
        if request.GET or len(request.body) > 4096:
            raise Denied
        data = json.loads(request.body, object_pairs_hook=duplicate_safe)
        request._body = b""  # No capability retained for error/debug serialization.
        return private_response(authorize(data))
    except (
        Denied,
        signing.BadSignature,
        ValueError,
        TypeError,
        KeyError,
        AttributeError,
        Reservation.DoesNotExist,
        TelegramActivation.DoesNotExist,
    ):
        return private_response({"authorized": False, "error": "authority_denied"}, 403)
