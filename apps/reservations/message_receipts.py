"""Read one backend proof using an ephemeral capability; never accept caller ACKs.

The source-render adapter remains closed until its independent contract is
installed. A receipt cannot establish a missing pre-attempt expectation.
"""

import json
import re
from uuid import UUID

import httpx
from django.conf import settings
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
    if isinstance(message, dict) and set(message) == {
        "mode",
        "template_ref",
        "variables",
        "components_sha256",
        "attachment",
    }:
        from .website_template_contract_v2 import content_digest as digest_v2

        return digest_v2(language, message)
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
                if len(raw) > (131072 if content or render_source else 32768):
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
    suffix = (
        "_review"
        if ":review:" in facts["source_event_key"]
        else "_payment_link"
        if ":payment_link:" in facts["source_event_key"]
        else ("_payment" if ":payment:" in facts["source_event_key"] else "")
    )
    family = (
        "lsa_ext_checkout"
        if facts.get("scenario") == "02" and facts.get("source_family") == "lsa_ext_checkout"
        else f"lsa{facts['scenario']}" + suffix
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


def historical_website_scope(row):
    if row.scenario not in {"07", "08"}:
        raise ValueError
    resource_kind = (
        "review"
        if row.message_type in {"review_followup_1", "review_followup_2", "review_followup_3"}
        else "payment_link"
        if row.message_type == "payment_link"
        else "payment"
        if row.financial
        else "core"
    )
    return {
        "scenario": row.scenario,
        "job_sha256": row.job_sha256,
        "source_event_key": f"website:{row.scenario}:{resource_kind}:{row.event_id}",
        "reservation_key": f"{'morning07' if row.scenario == '07' else 'dayuse08'}:{row.stay_id}",
        "reservation_revision": row.revision,
        "source_message_type": row.message_type,
    }


@sensitive_variables()
def reconcile(row_id, request_id, capability, *, expected_binding=None):
    # This capability is independently authenticated by19. It never reuses or
    # promotes a source READ capability to write authority.
    proof = fetch(request_id, capability)
    if expected_binding is not None and proof.get("binding_ref") != expected_binding:
        raise ValueError
    before = (
        proof.get("proof_stage") == "intake_held"
        and type(proof.get("attempts_claimed")) is int
        and proof["attempts_claimed"] == 0
        and proof.get("provider_attempted") is False
    )
    planned = fetch(request_id, capability, content=True) if before else None
    render_source = fetch(request_id, capability, render_source=True) if before else None
    from .website_loyalty_source_v2 import source_lock

    is_loyalty = relay.WebsiteMessageRelay.objects.filter(
        pk=row_id, message_type="loyalty_invitation"
    ).exists()
    with source_lock(is_loyalty=is_loyalty):
        row = relay.WebsiteMessageRelay.objects.select_for_update().get(pk=row_id)
        prior = (
            WebsiteMessageExpectation.objects.select_for_update()
            .filter(request_id=request_id)
            .first()
        )
        if prior:
            # A late signed receipt describes the immutable pre-attempt binding,
            # not authority for a new send. Pausing dispatch or ending/changing
            # the stay must not erase its history. Current payment/slot gates
            # still check the current recipient and booking separately.
            if prior.relay_id != row.pk:
                raise ValueError
            if row.scenario == "02":
                from .extension_checkout_source_v2 import historical_scope

                expected = historical_scope(row)
            else:
                expected = historical_website_scope(row)
            if any(prior.identity.get(key) != value for key, value in expected.items()):
                raise ValueError
            identity = verified_identity(proof, prior.identity, request_id)
        else:
            from .extension_checkout_source_v2 import enabled as extension_enabled

            if not (extension_enabled() if row.scenario == "02" else relay.enabled()):
                return "held"
            current = relay.current_event(row)
            if current is None:
                raise ValueError
            event, facts = current
            identity = verified_identity(proof, facts, request_id)
            language = facts["language"] if row.scenario == "02" else event.stay.guest_language
            if proof["language"] != language:
                raise ValueError
        if prior is None:
            version2 = (
                before
                and isinstance(render_source, dict)
                and render_source.get("contract_version") == "yc19-render-source-view-2"
            )
            if version2 and not getattr(settings, "MESSAGE_SOURCE_V2_ENABLED", False):
                return "held"
            if version2 and identity["waba_id"] != "2075336595945655":
                raise ValueError
            render_contract = (
                "yc19-render-source-view-2" if version2 else "yc19-render-source-view-1"
            )
            provenance = (
                "authenticated_source01_review+private_runtime_rpc_review_policy20"
                if version2
                and facts["source_message_type"]
                in {"review_followup_1", "review_followup_2", "review_followup_3"}
                else "authenticated_source01_financial"
                if version2
                and facts["source_message_type"]
                in {"payment_link", "payment_success", "loyalty_invitation"}
                else "private_runtime_rpc_source20"
            )
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
                or render_source.get("contract_version") != render_contract
                or render_source.get("request_id") != request_id
                or render_source.get("prepare_ref") != proof.get("prepare_ref")
                or render_source.get("prepared_at_epoch") != proof.get("prepared_at_epoch")
                or render_source.get("source_provenance") != provenance
                or render_source.get("domain_recheck_required") is not True
                or render_source.get("dispatch_allowed") is not False
            ):
                raise ValueError
            if not before or row.state != "forwarding" or facts["domain_authorized"] is not True:
                return "held"
            if version2:
                from .website_template_policy_v2 import policy_from_source

                policy = policy_from_source(event, facts, render_source["context"])
            else:
                policy = independent_render_policy(
                    event, facts, render_source.get("context") if before else None
                )
            if not policy:
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
            if version2:
                from .website_template_selector_v2 import PACK_SHA, SELECTION_REF

                if (
                    set(planned)
                    != {
                        "contract_version",
                        "request_id",
                        "prepare_ref",
                        "prepared_at_epoch",
                        "language",
                        "message_input",
                        "content_digest",
                        "selection_ref",
                        "source_pack_sha256",
                        "content_binding_basis",
                        "dispatch_allowed",
                    }
                    or planned.get("selection_ref") != SELECTION_REF
                    or planned.get("source_pack_sha256") != PACK_SHA
                ):
                    raise ValueError
            if (
                planned.get("contract_version")
                != ("yc19-prepared-content-2" if version2 else "yc19-prepared-content-1")
                or planned.get("request_id") != request_id
                or planned.get("prepare_ref") != prepare_ref
                or planned.get("prepared_at_epoch") != prepared_at
                or planned.get("language") != policy["language"]
                or planned.get("language") != proof["language"]
                or planned.get("message_input") != policy["message"]
                or planned.get("content_binding_basis")
                != (
                    "planned_pending_independent_source_template_verification"
                    if version2
                    else "planned_pending_source_render_verification"
                )
                or planned.get("dispatch_allowed") is not False
                or not constant_time_compare(digest, identity["content_digest"])
                or planned.get("content_digest") != digest
            ):
                raise ValueError
            expectation = WebsiteMessageExpectation.objects.create(
                request_id=request_id,
                relay=row,
                identity=identity,
                prepare_ref=prepare_ref,
                prepared_at_epoch=prepared_at,
                render_policy_ref=policy["render_policy_ref"],
            )
            from .website_loyalty_source_v2 import consume_ack

            consume_ack(expectation)
            from .website_review_source_v2 import consume_ack as consume_review_ack

            consume_review_ack(expectation)
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
            if prior.state == "delivered" and delivered is True and state == "read":
                prior.state = "read"
                prior.observed_at_epoch = observed
                prior.receipt_evidence_ref = evidence
                prior.save(update_fields=["state", "observed_at_epoch", "receipt_evidence_ref"])
            from .website_financial_receipts_v2 import project_payment_link_receipt

            project_payment_link_receipt(prior)
            from .website_loyalty_source_v2 import project_receipt

            project_receipt(prior)
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
        from .website_financial_receipts_v2 import project_payment_link_receipt

        project_payment_link_receipt(prior)
        from .website_loyalty_source_v2 import project_receipt

        project_receipt(prior)
        return prior.state


@sensitive_variables()
def initial_binding_relay(request_id, binding_ref, capability):
    """Resolve only an existing forwarding journal from authenticated zero-claim proof."""
    proof = fetch(request_id, capability)
    if (
        set(proof) != PROOF_FIELDS
        or proof.get("contract_version") != "yc19-receipt-proof-1"
        or proof.get("source") != "backend19"
        or proof.get("request_id") != request_id
        or proof.get("binding_ref") != binding_ref
        or proof.get("proof_stage") != "intake_held"
        or type(proof.get("attempts_claimed")) is not int
        or proof["attempts_claimed"] != 0
        or proof.get("provider_attempted") is not False
        or proof.get("provider_message_id") is not None
        or proof.get("wamid") is not None
        or proof.get("dispatch_allowed") is not False
    ):
        raise ValueError
    scenario, event_key = proof.get("scenario"), proof.get("source_event_key")
    if scenario == "02":
        from .extension_checkout_source_v2 import initial_relay

        return initial_relay(proof)
    if scenario not in {"07", "08"} or not isinstance(event_key, str):
        raise ValueError
    match = re.fullmatch(
        rf"website:{scenario}:(core|payment|payment_link|review):([0-9a-f-]{{36}})", event_key
    )
    if not match or not isinstance(proof.get("reservation_key"), str):
        raise ValueError
    kind, event_id = match.groups()
    event_id = UUID(event_id)
    prefix = "morning07:" if scenario == "07" else "dayuse08:"
    if not proof["reservation_key"].startswith(prefix):
        raise ValueError
    stay_id = UUID(proof["reservation_key"][len(prefix) :])
    if (
        type(proof.get("reservation_revision")) is not str
        or not isinstance(proof.get("job_sha256"), str)
        or not re.fullmatch(r"[a-f0-9]{64}", proof["job_sha256"])
    ):
        raise ValueError
    rows = list(
        relay.WebsiteMessageRelay.objects.filter(
            scenario=scenario,
            stay_id=stay_id,
            event_id=event_id,
            financial=kind == "payment",
            message_type=proof.get("source_message_type"),
            revision=proof["reservation_revision"],
            job_sha256=proof["job_sha256"],
            state="forwarding",
        ).values_list("pk", flat=True)[:2]
    )
    if len(rows) != 1:
        raise ValueError
    return rows[0]


@csrf_exempt  # No cookie/session authority. Only one-request backend proof capability.
@sensitive_variables()
def receipt(request):
    request._disable_google_integrations = True
    capability = request.META.pop("HTTP_X_LSA_RECEIPT_CAPABILITY", "")
    request.__dict__.pop("headers", None)
    if not getattr(settings, "MESSAGE_RECEIPTS_ENABLED", False) or not getattr(
        settings, "MESSAGE_SOURCE_AUTH_ENABLED", False
    ):
        return source.response({"code": "unavailable"}, 404)
    if request.method != "POST":
        return source.response({"code": "method_not_allowed"}, 405)
    if request.GET or request.content_type != "application/json" or len(request.body) > 512:
        return source.response({"code": "invalid_request"}, 400)
    try:
        body = json.loads(request.body, object_pairs_hook=source.unique_object)
        if (
            not isinstance(body, dict)
            or set(body)
            not in (
                {"request_id", "relay_ref"},
                {"request_id", "expectation_ref"},
                {"request_id", "binding_ref"},
            )
            or not isinstance(body["request_id"], str)
            or not re.fullmatch(REQUEST_PATTERN, body["request_id"])
            or not isinstance(capability, str)
            or not re.fullmatch(r"[A-Za-z0-9_-]{43}", capability)
        ):
            raise ValueError
        expected_binding = None
        if "binding_ref" in body:
            expected_binding = body["binding_ref"]
            if not isinstance(expected_binding, str) or not re.fullmatch(
                r"[A-Za-z0-9_.:-]{1,128}", expected_binding
            ):
                raise ValueError
            row_id = initial_binding_relay(body["request_id"], expected_binding, capability)
        elif "expectation_ref" in body:
            prior = WebsiteMessageExpectation.objects.filter(
                expectation_ref=UUID(body["expectation_ref"]), request_id=body["request_id"]
            ).first()
            if prior is None:
                raise ValueError
            row_id = prior.relay_id
        else:
            row_id = UUID(body["relay_ref"])
        if not relay.WebsiteMessageRelay.objects.filter(pk=row_id).exists():
            raise ValueError
        state = reconcile(row_id, body["request_id"], capability, expected_binding=expected_binding)
    except Exception:
        return source.response({"code": "proof_unverified", "dispatch_allowed": False}, 409)
    return source.response({"code": state, "dispatch_allowed": False})


def core_slot_delivered(
    stay, event, scenario, kind, *, allow_legacy_lookup=False, reconcile_uncertain=False
):
    from . import legacy_message_receipts

    keys = source.core_keys(stay.pk, event.payload, scenario)
    key = keys.get(kind)
    if not key:
        return False
    phone = source.normalize_phone_number(stay.guest_phone)
    recipient = (
        "site01:"
        + source.salted_hmac(
            "website-source-recipient.v1", f"{scenario}:{stay.pk}:{phone}", algorithm="sha256"
        ).hexdigest()
    )
    if getattr(settings, "MESSAGE_RECEIPTS_ENABLED", False):
        candidates = WebsiteMessageExpectation.objects.filter(
            relay__scenario=scenario,
            relay__stay_id=stay.pk,
            relay__financial=False,
            relay__job_sha256=source.sha(key),
            state__in=["delivered", "read"],
            attempts_claimed=1,
        )
        if any(
            row.identity.get("recipient_ref") == recipient
            and row.identity.get("source_message_type") == kind
            and row.provider_message_id
            and row.wamid
            and row.receipt_evidence_ref
            for row in candidates
        ):
            return True
    model = source.SOURCES[scenario][3]
    old = model.objects.filter(
        pk=source.legacy_reference(key, scenario),
        stay=stay,
        state__in=["accepted", "posting", "uncertain"] if reconcile_uncertain else ["accepted"],
    ).first()
    if not old or not legacy_message_receipts.bound_before_cutover(old):
        return False
    rows = legacy_message_receipts.cached_rows(old)
    if not rows and allow_legacy_lookup:
        from . import day_use08_gateway, morning07_gateway

        gateway = morning07_gateway if scenario == "07" else day_use08_gateway
        rows = gateway.exact_receipt_messages(old)
    return any(legacy_message_receipts.delivered(row) for row in rows)


def predecessors_delivered(stay, event, scenario, kind):
    # Source READ and relay checks are database-only. A bound ACK is not delivery.
    status = "status_" + event.payload["event_type"]
    required = {"house_rules": [status], "checkin_guide": [status, "house_rules"]}.get(kind, [])
    return all(core_slot_delivered(stay, event, scenario, predecessor) for predecessor in required)


def core_delivered(stay, revision, scenario, *, reconcile_uncertain=False):
    event = stay.communication_events.filter(revision=revision).first()
    if not event:
        return False
    if not source.current_core(stay, event, source.SOURCES[scenario][4]):
        return False
    return all(
        core_slot_delivered(
            stay,
            event,
            scenario,
            kind,
            allow_legacy_lookup=True,
            reconcile_uncertain=reconcile_uncertain,
        )
        for kind in source.core_keys(stay.pk, event.payload, scenario)
        if kind != "reminder_checkout"
    )
