"""Read one backend proof using an ephemeral capability; never accept caller ACKs.

The source-render adapter remains closed until its independent contract is
installed. A receipt cannot establish a missing pre-attempt expectation.
"""

import json
import re
from uuid import UUID

import httpx
from django.conf import settings
from django.db import transaction
from django.utils import timezone
from django.utils.crypto import constant_time_compare
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.debug import sensitive_variables

from . import message_source_authorization as source
from . import message_source_relay as relay
from .message_receipt_models import WebsiteMessageExpectation

ORIGIN = "https://sma-ycloud-test.onrender.com"
HEADER = "X-LSA-Receipt-Capability"
REQUEST_PATTERN = r"n8n20-[0-9a-f]{64}"
IDENTITY_FIELDS = (
    "request_id",
    "binding_ref",
    "source_family",
    "job_sha256",
    "source_event_key",
    "scenario",
    "reservation_key",
    "reservation_revision",
    "recipient_ref",
    "purpose",
    "source_message_type",
    "content_digest",
    "business_sender",
    "waba_id",
    "phone_id",
    "language",
)
PROOF_FIELDS = set(IDENTITY_FIELDS) | {
    "contract_version",
    "source",
    "provider_message_id",
    "wamid",
    "transport",
    "state",
    "delivered",
    "receipt_evidence_ref",
    "observed_at_epoch",
    "proof_stage",
    "attempts_claimed",
    "provider_attempted",
    "prepare_ref",
    "prepared_at_epoch",
    "attempt_count_basis",
    "dispatch_allowed",
}


def independent_render_policy(event, facts, context=None):
    from .website_message_policy import policy_from_source

    # Owner20's private RPC is independent of planned content. This adapter
    # proves content only; provider approval and consent remain separate gates.
    return policy_from_source(event, facts, context)


def content_digest(language, message):
    if not isinstance(message, dict):
        raise ValueError
    if message.get("mode") == "text" and set(message) == {"mode", "text"}:
        if not isinstance(message["text"], str) or not 1 <= len(message["text"]) <= 4096:
            raise ValueError
        normalized = {"mode": "text", "text": message["text"]}
    elif message.get("mode") == "template" and set(message) == {
        "mode",
        "template_ref",
        "variables",
    }:
        if (
            not isinstance(message["template_ref"], str)
            or not message["template_ref"]
            or not isinstance(message["variables"], list)
            or len(message["variables"]) > 40
            or any(not isinstance(v, str) for v in message["variables"])
        ):
            raise ValueError
        normalized = {
            "mode": "template",
            "template_ref": message["template_ref"],
            "variables": message["variables"],
        }
    else:
        raise ValueError
    return source.sha(json.dumps([language, normalized], ensure_ascii=False, separators=(",", ":")))


@sensitive_variables()
def fetch(request_id, capability, *, content=False, render_source=False):
    view = "render-source" if render_source else "content"
    path = (
        f"/v1/automation/prepared-intents/{request_id}/{view}/"
        if content or render_source
        else f"/v1/automation/receipts/{request_id}/"
    )
    with httpx.Client(timeout=httpx.Timeout(8, connect=3), follow_redirects=False) as client:
        with client.stream("GET", ORIGIN + path, headers={HEADER: capability}) as reply:
            if reply.status_code != 200:
                raise ValueError
            raw = bytearray()
            for chunk in reply.iter_bytes():
                raw.extend(chunk)
                if len(raw) > 32768:
                    raise ValueError
    value = json.loads(raw, object_pairs_hook=source.unique_object)
    if not isinstance(value, dict):
        raise ValueError
    return value


def verified_identity(proof, facts, request_id):
    if (
        set(proof) != PROOF_FIELDS
        or proof.get("source") != "backend19"
        or proof.get("language") not in {"ar", "en", "fr"}
        or proof.get("dispatch_allowed") is not False
        or proof.get("attempt_count_basis") != "durable_claim_not_observed_post_count"
        or not isinstance(proof.get("receipt_evidence_ref"), str)
    ):
        raise ValueError
    identity = {key: proof.get(key) for key in IDENTITY_FIELDS}
    if any(not isinstance(value, str) or not 1 <= len(value) <= 512 for value in identity.values()):
        raise ValueError
    if type(proof.get("reservation_revision")) is not str:
        raise ValueError
    family = f"lsa{facts['scenario']}" + (
        "_payment" if ":payment:" in facts["source_event_key"] else ""
    )
    expected = {
        "request_id": request_id,
        "source_family": family,
        **{
            key: facts[key]
            for key in (
                "scenario",
                "job_sha256",
                "source_event_key",
                "reservation_key",
                "reservation_revision",
                "recipient_ref",
                "purpose",
                "source_message_type",
            )
        },
    }
    if any(identity[key] != value for key, value in expected.items()):
        raise ValueError
    if (
        proof.get("contract_version") != "yc19-receipt-proof-1"
        or proof.get("transport") != "ycloud"
        or not re.fullmatch(r"[0-9a-f]{64}", identity["content_digest"])
    ):
        raise ValueError
    return identity


def expectation_snapshot(row):
    return {
        "contract_version": "yc19-source-expectation-1",
        "prepare_ref": row.prepare_ref,
        "request_id": row.request_id,
        "binding_ref": row.identity["binding_ref"],
        "content_digest": row.identity["content_digest"],
        **{
            key: row.identity[key]
            for key in ("reservation_key", "reservation_revision", "recipient_ref", "purpose")
        },
        "expectation_ref": str(row.expectation_ref),
        "registered_before_attempt": True,
    }


def projected_expectation(facts):
    row = WebsiteMessageExpectation.objects.filter(
        relay__scenario=facts["scenario"],
        relay__event_id=facts["source_event_key"].rsplit(":", 1)[-1],
        relay__job_sha256=facts["job_sha256"],
    ).first()
    if row and all(
        row.identity.get(k) == facts[k]
        for k in (
            "source_event_key",
            "reservation_key",
            "reservation_revision",
            "recipient_ref",
            "purpose",
            "source_message_type",
        )
    ):
        return expectation_snapshot(row)
    return None


@sensitive_variables()
def reconcile(row_id, request_id, capability):
    # This capability is independently authenticated by19. It never reuses or
    # promotes a source READ capability to write authority.
    proof = fetch(request_id, capability)
    before = (
        proof.get("proof_stage") == "intake_held"
        and type(proof.get("attempts_claimed")) is int
        and proof["attempts_claimed"] == 0
        and proof.get("provider_attempted") is False
    )
    planned = fetch(request_id, capability, content=True) if before else None
    render_source = fetch(request_id, capability, render_source=True) if before else None
    with transaction.atomic():
        row = relay.WebsiteMessageRelay.objects.select_for_update().get(pk=row_id)
        current = relay.current_event(row)
        if current is None:
            raise ValueError
        event, facts = current
        identity = verified_identity(proof, facts, request_id)
        if proof["language"] != event.stay.guest_language:
            raise ValueError
        prior = (
            WebsiteMessageExpectation.objects.select_for_update()
            .filter(request_id=request_id)
            .first()
        )
        if prior is None:
            # Existing attempts, unknown posts, legacy receipts and held old
            # intents can never be adopted as a new source expectation.
            if before and (
                not isinstance(render_source, dict)
                or set(render_source)
                != {
                    "contract_version",
                    "request_id",
                    "prepare_ref",
                    "prepared_at_epoch",
                    "context",
                    "source_provenance",
                    "domain_recheck_required",
                    "dispatch_allowed",
                }
                or render_source.get("contract_version") != "yc19-render-source-view-1"
                or render_source.get("request_id") != request_id
                or render_source.get("prepare_ref") != proof.get("prepare_ref")
                or render_source.get("prepared_at_epoch") != proof.get("prepared_at_epoch")
                or render_source.get("source_provenance") != "private_runtime_rpc_source20"
                or render_source.get("domain_recheck_required") is not True
                or render_source.get("dispatch_allowed") is not False
            ):
                raise ValueError
            policy = independent_render_policy(
                event, facts, render_source.get("context") if before else None
            )
            if not before or not policy or row.state != "forwarding":
                return "held"
            now = int(timezone.now().timestamp())
            prepared_at = proof.get("prepared_at_epoch")
            prepare_ref = proof.get("prepare_ref")
            if (
                type(prepared_at) is not int
                or not now - 300 <= prepared_at <= now
                or not isinstance(prepare_ref, str)
                or not re.fullmatch(r"[0-9a-f]{64}", prepare_ref)
                or proof.get("provider_message_id")
                or proof.get("wamid")
                or proof.get("delivered") is not False
                or not isinstance(policy, dict)
                or set(policy) != {"language", "message", "render_policy_ref"}
                or not isinstance(policy["render_policy_ref"], str)
                or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", policy["render_policy_ref"])
            ):
                raise ValueError
            digest = content_digest(policy["language"], policy["message"])
            if (
                planned.get("contract_version") != "yc19-prepared-content-1"
                or planned.get("request_id") != request_id
                or planned.get("prepare_ref") != prepare_ref
                or planned.get("prepared_at_epoch") != prepared_at
                or planned.get("language") != policy["language"]
                or planned.get("language") != proof["language"]
                or planned.get("message_input") != policy["message"]
                or planned.get("content_binding_basis")
                != "planned_pending_source_render_verification"
                or planned.get("dispatch_allowed") is not False
                or not constant_time_compare(digest, identity["content_digest"])
                or planned.get("content_digest") != digest
            ):
                raise ValueError
            WebsiteMessageExpectation.objects.create(
                request_id=request_id,
                relay=row,
                identity=identity,
                prepare_ref=prepare_ref,
                prepared_at_epoch=prepared_at,
                render_policy_ref=policy["render_policy_ref"],
            )
            return "prepared"
        if (
            prior.relay_id != row.pk
            or prior.identity != identity
            or prior.prepare_ref != proof.get("prepare_ref")
            or prior.prepared_at_epoch != proof.get("prepared_at_epoch")
        ):
            raise ValueError
        claims = proof.get("attempts_claimed")
        if type(claims) is not int or claims not in {0, 1} or claims < prior.attempts_claimed:
            raise ValueError
        if claims == 0:
            return "prepared" if before else "held"
        if proof.get("proof_stage") != "attempt_claimed":
            raise ValueError
        provider_id, wamid = proof.get("provider_message_id"), proof.get("wamid")
        if any(
            value is not None and (not isinstance(value, str) or len(value) > 512)
            for value in (provider_id, wamid)
        ):
            raise ValueError
        if prior.provider_message_id and prior.provider_message_id != provider_id:
            raise ValueError
        if prior.wamid and prior.wamid != wamid:
            raise ValueError
        if (provider_id and proof.get("provider_attempted") is not True) or (
            not provider_id and proof.get("provider_attempted") is not None
        ):
            raise ValueError
        delivered = proof.get("delivered")
        state = proof.get("state")
        observed = proof.get("observed_at_epoch")
        evidence = proof.get("receipt_evidence_ref")
        if delivered is True:
            if (
                state not in {"delivered", "read"}
                or not provider_id
                or not wamid
                or proof.get("provider_attempted") is not True
                or not isinstance(evidence, str)
                or not 1 <= len(evidence) <= 512
                or type(observed) is not int
                or not prior.prepared_at_epoch <= observed <= int(timezone.now().timestamp()) + 30
            ):
                raise ValueError
        elif delivered is not False or state in {"delivered", "read"}:
            raise ValueError
        if prior.state in {"delivered", "read"}:
            return prior.state  # Never downgrade a trusted delivery.
        prior.attempts_claimed = claims
        prior.provider_message_id, prior.wamid = provider_id or "", wamid or ""
        prior.state = state if delivered is True else "accepted" if provider_id else "uncertain"
        prior.receipt_evidence_ref = evidence if delivered is True else ""
        prior.observed_at_epoch = observed if delivered is True else None
        prior.save(
            update_fields=[
                "attempts_claimed",
                "provider_message_id",
                "wamid",
                "state",
                "receipt_evidence_ref",
                "observed_at_epoch",
            ]
        )
        return prior.state


@csrf_exempt  # No cookie/session authority. Only one-request backend proof capability.
@sensitive_variables()
def receipt(request):
    request._disable_google_integrations = True
    capability = request.META.pop("HTTP_X_LSA_RECEIPT_CAPABILITY", "")
    request.__dict__.pop("headers", None)
    if not getattr(settings, "MESSAGE_RECEIPTS_ENABLED", False) or not relay.enabled():
        return source.response({"code": "unavailable"}, 404)
    if request.method != "POST":
        return source.response({"code": "method_not_allowed"}, 405)
    if request.GET or request.content_type != "application/json" or len(request.body) > 512:
        return source.response({"code": "invalid_request"}, 400)
    try:
        body = json.loads(request.body, object_pairs_hook=source.unique_object)
        if (
            not isinstance(body, dict)
            or set(body) != {"request_id", "relay_ref"}
            or not isinstance(body["request_id"], str)
            or not re.fullmatch(REQUEST_PATTERN, body["request_id"])
            or not isinstance(capability, str)
            or not re.fullmatch(r"[A-Za-z0-9_-]{43}", capability)
        ):
            raise ValueError
        row_id = UUID(body["relay_ref"])
        if not relay.WebsiteMessageRelay.objects.filter(pk=row_id).exists():
            raise ValueError
        state = reconcile(row_id, body["request_id"], capability)
    except Exception:
        return source.response({"code": "proof_unverified", "dispatch_allowed": False}, 409)
    return source.response({"code": state, "dispatch_allowed": False})


def core_delivered(stay, revision, scenario):
    if not getattr(settings, "MESSAGE_RECEIPTS_ENABLED", False):
        return False
    event = stay.communication_events.filter(revision=revision).first()
    if not event:
        return False
    if not source.current_core(stay, event, source.SOURCES[scenario][4]):
        return False
    keys = source.core_keys(stay.pk, event.payload, scenario)
    phone = source.normalize_phone_number(stay.guest_phone)
    recipient = (
        "site01:"
        + source.salted_hmac(
            "website-source-recipient.v1", f"{scenario}:{stay.pk}:{phone}", algorithm="sha256"
        ).hexdigest()
    )
    for kind, key in keys.items():
        if kind == "reminder_checkout":
            continue
        candidates = WebsiteMessageExpectation.objects.filter(
            relay__scenario=scenario,
            relay__stay_id=stay.pk,
            relay__financial=False,
            relay__job_sha256=source.sha(key),
            state__in=["delivered", "read"],
            attempts_claimed=1,
        )
        if not any(
            row.identity.get("recipient_ref") == recipient
            and row.identity.get("source_message_type") == kind
            and row.provider_message_id
            and row.wamid
            and row.receipt_evidence_ref
            for row in candidates
        ):
            return False
    return True
