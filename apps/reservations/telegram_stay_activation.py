"""Operations-only same-day activation, using independent local phone proof."""

import uuid
from datetime import UTC, datetime

from django.conf import settings
from django.db import transaction
from django.shortcuts import get_object_or_404
from django.urls import reverse
from django.utils import timezone

from apps.accounts.access import (
    MANAGER_SESSION_KEY,
    has_operations_access,
    operations_access_required,
)
from apps.core.phone_numbers import normalize_phone_number
from apps.notifications.models import AuditLog

from . import message_source_authorization as source
from . import telegram_activation as ordinary
from . import telegram_authority as authority
from . import telegram_backend as backend
from .telegram_copy import UI
from .telegram_stay_models import TelegramStayActivation
from .telegram_stay_transport import selected


def key_for(stay, scenario):
    return ("morning07:" if scenario == "07" else "dayuse08:") + str(stay.pk)


def eligible(stay):
    return bool(
        getattr(settings, "TELEGRAM_STAY_MESSAGES_ENABLED", False)
        and stay.environment == "production"
        and stay.status != "cancelled"
        and stay.ends_at > timezone.now()
        and selected(stay)
    )


class Backend(backend.Backend):
    def health(self):
        if self.ready_checked:
            return
        value = self.call("health")
        if (
            value.get("contract") != "tg39_stay_transport_v1"
            or value.get("ready") is not True
            or value.get("dispatch_enabled") is not True
            or value.get("cutover_configured") is not True
        ):
            raise backend.NotReady
        self.ready_checked = True


def scope_for(request, stay, scenario, row=None, phone=None):
    if not has_operations_access(request) or not eligible(stay):
        raise authority.Denied
    value = {
        "kind": "stay_operations",
        "scenario": scenario,
        "stay": str(stay.pk),
        "actor": request.user.pk,
        "grant": request.session.get(MANAGER_SESSION_KEY),
        "key": key_for(stay, scenario),
    }
    if row:
        if row.actor_id != request.user.pk or authority.phone_digest(phone) != row.phone_digest:
            raise authority.Denied
        value.update(
            proof=str(row.proof_id), phone=phone, evidence_ref="admin_verified:" + str(row.proof_id)
        )
    return value


def authorize(scope, action, key, data, claims):
    scenario = scope["scenario"]
    if scenario not in source.SOURCES or action not in {
        "health",
        "status",
        "prepare",
        "approve_phone",
        "issue_invitation",
    }:
        raise authority.Denied
    model = source.SOURCES[scenario][0]
    with transaction.atomic():
        stay = model.objects.select_for_update().get(pk=scope["stay"])
        actor = authority.actor_allowed({**scope, "kind": "operations"}, stay)
        if not eligible(stay) or key_for(stay, scenario) != key:
            raise authority.Denied
        result = {
            "contract": "tg39_activation_authority_v1",
            "authorized": True,
            "request_id": data["request_id"],
            "action": action,
            "reservation_key": key,
            "expires_at": datetime.fromtimestamp(claims["expires"], UTC).isoformat(),
        }
        if action in authority.WRITES:
            row = TelegramStayActivation.objects.select_for_update().get(
                scenario=scenario, stay_id=stay.pk
            )
            field = authority.WRITES[action]
            phone = scope.get("phone", "")
            if (
                row.state != "processing"
                or row.actor_id != actor.pk
                or str(row.proof_id) != scope.get("proof")
                or getattr(row, field) is not None
                or data["request_id"] != str(uuid.uuid5(row.proof_id, action))
                or authority.phone_digest(phone) != row.phone_digest
                or phone != normalize_phone_number(stay.guest_phone)
                or action == "issue_invitation"
                and row.approve_authorized_at is None
            ):
                raise authority.Denied
            setattr(row, field, timezone.now())
            row.save(update_fields=[field])
            if action == "approve_phone":
                result.update(
                    phone_proof_verified=True,
                    phone=phone.lstrip("+"),
                    evidence_ref="admin_verified:" + str(row.proof_id),
                )
        return result


def context(request, stay, scenario):
    if not eligible(stay):
        return None
    request._disable_google_integrations = True
    value = {
        "admin": True,
        "state": "unavailable",
        "can_issue": False,
        "copy": UI["ar"],
        "form": ordinary.AdminProofForm(),
        "action_url": reverse("notifications:telegram_stay_activation", args=[scenario, stay.pk]),
    }
    client = None
    try:
        client = Backend()
        client.authority_context = scope_for(request, stay, scenario)
        value["state"] = client.status(key_for(stay, scenario))
        row = TelegramStayActivation.objects.filter(scenario=scenario, stay_id=stay.pk).first()
        if row and value["state"] not in {"bound", "active"}:
            if row.state in {"uncertain", "processing"}:
                value["state"] = row.state
            elif row.state == "issued" and row.expires_at and row.expires_at > timezone.now():
                value["state"] = "active"
        value["can_issue"] = value["state"] in {"unverified", "expired", "hold"}
    except (backend.Unavailable, authority.Denied):
        pass
    finally:
        if client:
            client.close()
    value["message"] = ordinary.LABELS[value["state"]]
    return value


def issue(request, stay, scenario):
    if not eligible(stay):
        return {"state": "closed", "url": ""}
    form = ordinary.AdminProofForm(request.POST)
    if not form.is_valid():
        return {"state": "unverified", "url": "", "form": form}
    phone = form.cleaned_data["phone"]
    if phone != normalize_phone_number(stay.guest_phone):
        return {"state": "unverified", "url": ""}
    with transaction.atomic():
        current = source.SOURCES[scenario][0].objects.select_for_update().get(pk=stay.pk)
        if not eligible(current) or normalize_phone_number(current.guest_phone) != phone:
            raise authority.Denied
        row = TelegramStayActivation.objects.filter(scenario=scenario, stay_id=stay.pk).first()
        if row and (
            row.state in {"processing", "uncertain", "bound"}
            or row.state == "issued"
            and row.expires_at
            and row.expires_at > timezone.now()
        ):
            return {"state": "active" if row.state == "issued" else row.state, "url": ""}
        row = row or TelegramStayActivation(scenario=scenario, stay_id=stay.pk)
        row.actor, row.state = request.user, "processing"
        row.evidence_ref = form.cleaned_data["evidence_ref"]
        row.phone_digest, row.proof_id = authority.phone_digest(phone), uuid.uuid4()
        row.approve_authorized_at = row.issue_authorized_at = row.expires_at = None
        row.save()
        AuditLog.objects.create(
            actor_user=request.user,
            action="telegram.stay_phone_proof",
            object_type="TelegramStayActivation",
            object_reference=str(row.pk),
            summary="Independent booking-specific phone proof; no guest message.",
            metadata={"scenario": scenario, "proof_id": str(row.proof_id)},
        )
    client = None
    result = {"state": "uncertain", "url": ""}
    try:
        client = Backend()
        client.authority_context = scope_for(request, stay, scenario, row, phone)
        key = key_for(stay, scenario)
        client.prepare(key)
        approved = client.rpc(
            "tg20_s01_approve_phone",
            {
                "p_key": key,
                "p_phone": phone.lstrip("+"),
                "p_evidence_ref": "admin_verified:" + str(row.proof_id),
            },
        )
        if approved["action"] == "approved":
            issued = client.rpc("tg20_s01_issue_invitation", {"p_key": key})
            if issued["action"] == "issued":
                url, expiry = backend.checked_invitation(
                    issued, prefixes="h" if scenario == "07" else "d"
                )
                result = {"state": "issued", "url": url, "expires_at": expiry}
            else:
                result = {"state": "bound" if issued["action"] == "bound" else "hold", "url": ""}
        else:
            result = {"state": "hold", "url": ""}
    except (backend.NotReady, authority.Denied):
        result = {"state": "hold", "url": ""}
    except backend.Unavailable:
        pass
    finally:
        if client:
            client.close()
    TelegramStayActivation.objects.filter(pk=row.pk).update(
        state=result["state"], expires_at=result.get("expires_at"), updated_at=timezone.now()
    )
    return result


@operations_access_required
def activate(request, scenario, stay_id):
    if scenario not in source.SOURCES:
        from django.http import Http404

        raise Http404
    stay = get_object_or_404(
        source.SOURCES[scenario][0].objects.select_related("property"), pk=stay_id
    )
    if request.method != "POST":
        from django.http import HttpResponseNotAllowed

        return HttpResponseNotAllowed(["POST"])
    try:
        result = issue(request, stay, scenario)
    except authority.Denied:
        result = {"state": "hold", "url": ""}
    return ordinary.result_response(request, result)
