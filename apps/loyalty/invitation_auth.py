"""Dedicated invitation-only credentials; secret appears once to the owner."""

import hashlib
import re
import secrets
from datetime import timedelta
from functools import wraps
from uuid import uuid4

from django.contrib import admin
from django.core import signing
from django.core.exceptions import ValidationError
from django.db import DatabaseError, IntegrityError, transaction
from django.http import JsonResponse
from django.shortcuts import render
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods

from apps.accounts.access import operations_access_required, require_operations_owner
from apps.notifications.models import AuditLog
from apps.reservations.sama_booking_api import response

from .models import InvitationAPICredential

SETUP_SALT = "loyalty.invitation-credential-setup.v1"


def authenticated(method):
    def decorate(view):
        @csrf_exempt
        @wraps(view)
        def secured(request, *args, **kwargs):
            if not request.is_secure():
                return response("https_required", 403)
            header = request.headers.get("Authorization", "")
            if not re.fullmatch(r"Bearer [0-9a-f]{64}", header):
                return response("unauthorized", 401)
            digest = hashlib.sha256(header[7:].encode()).hexdigest()
            try:
                valid = InvitationAPICredential.objects.filter(
                    key_sha256=digest, expires_at__gt=timezone.now(), revoked_at__isnull=True
                ).exists()
                if not valid:
                    return response("unauthorized", 401)
                if request.method != method:
                    return response("method_not_allowed", 405)
                if method == "POST" and (
                    request.content_type != "application/json" or len(request.body) > 8192
                ):
                    return response("invalid_request", 400)
                return view(request, *args, **kwargs)
            except DatabaseError:
                return response("coordination_unavailable", 503)
            except Exception:
                # Never disclose tokens, submitted contact data or provider payloads.
                return response("review_required", 503)

        return secured

    return decorate


@operations_access_required
@require_http_methods(["GET", "POST"])
def setup(request):
    require_operations_owner(request.user)
    if request.method == "POST":
        try:
            proof = signing.loads(
                request.POST.get("confirmation", ""), salt=SETUP_SALT, max_age=600
            )
            if proof["operator"] != request.user.pk:
                raise ValidationError("wrong_owner")
            if proof.get("action") == "revoke":
                with transaction.atomic():
                    InvitationAPICredential.objects.filter(
                        pk=proof["id"], revoked_at__isnull=True
                    ).update(revoked_at=timezone.now())
                    AuditLog.objects.create(
                        actor_user=request.user,
                        action="loyalty.invitation_api_key_revoked",
                        object_type="InvitationAPICredential",
                        object_reference=proof["id"],
                        summary="إلغاء اعتماد واجهة دعوات الولاء",
                    )
                return response("revoked")
            if InvitationAPICredential.objects.filter(pk=proof["id"]).exists():
                return response("setup_already_consumed", 409)
            token = secrets.token_hex(32)
            expiry = timezone.now() + timedelta(days=90)
            InvitationAPICredential.objects.create(
                id=proof["id"],
                key_sha256=hashlib.sha256(token.encode()).hexdigest(),
                created_by=request.user,
                expires_at=expiry,
            )
            AuditLog.objects.create(
                actor_user=request.user,
                action="loyalty.invitation_api_key_created",
                object_type="InvitationAPICredential",
                object_reference=proof["id"],
                summary="إنشاء اعتماد مخصص لواجهة دعوات الولاء؛ القيمة غير محفوظة في السجل",
            )
        except IntegrityError:
            return response("setup_already_consumed", 409)
        except (signing.BadSignature, KeyError, ValidationError):
            return response("invalid_setup_confirmation", 400)
        result = JsonResponse(
            {
                "name": "LSA Loyalty Invitations v1",
                "type": "httpHeaderAuth",
                "data": {"name": "Authorization", "value": "Bearer " + token},
                "scope": "loyalty-invitations-v1",
                "expires_at": expiry.isoformat(),
                "website_credential_id": proof["id"],
            }
        )
        result["Content-Disposition"] = 'attachment; filename="lsa-loyalty-credential.json"'
        result["Cache-Control"] = "no-store"
        result["Referrer-Policy"] = "no-referrer"
        return result
    result = render(
        request,
        "admin/loyalty/invitation_integration_setup.html",
        {
            **admin.site.each_context(request),
            "title": "إعداد اعتماد واجهة دعوات الولاء",
            "confirmation": signing.dumps(
                {"id": str(uuid4()), "operator": request.user.pk, "action": "issue"},
                salt=SETUP_SALT,
            ),
            "credentials": [
                {
                    "id": str(row.pk),
                    "expires_at": row.expires_at,
                    "confirmation": signing.dumps(
                        {"id": str(row.pk), "operator": request.user.pk, "action": "revoke"},
                        salt=SETUP_SALT,
                    ),
                }
                for row in InvitationAPICredential.objects.filter(
                    revoked_at__isnull=True, expires_at__gt=timezone.now()
                )
            ],
        },
    )
    result["Cache-Control"] = "no-store"
    return result
