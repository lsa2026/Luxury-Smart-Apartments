"""Dedicated Telegram ordering and immutable API-acceptance evidence, never read receipts."""

import hashlib
import json
import re
import uuid
from contextlib import nullcontext
from datetime import timedelta
from zoneinfo import ZoneInfo

from django.conf import settings
from django.core import signing
from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from django.views.decorators.debug import sensitive_variables

from apps.loyalty.invitation_auth import authenticated

from . import telegram_stay_authority as bridge
from . import telegram_stay_render as render
from . import telegram_workgroup as workgroup
from .telegram_authority import Denied, duplicate_safe, private_endpoint, private_response
from .telegram_backend import Unavailable
from .telegram_stay_models import TelegramStayChannel, TelegramStayMessage

CORE = ("status_new", "house_rules", "checkin_guide")
HEX = re.compile(r"[a-f0-9]{64}")


class ReceiptPagePending(Unavailable):
    pass


def identity(packet, purpose, sequence):
    booking = packet["booking"]
    prefix = "tg39wg" if workgroup.enabled() else "tg39"
    key = (
        f"{prefix}:{packet['scenario']}:{packet['source_key']}:r{packet['source_revision']}:"
        f"{packet['event_id']}:{purpose}"
    )
    return {
        **{
            field: packet[field]
            for field in (
                "source_owner",
                "source_key",
                "hostaway_reservation_id",
                "scenario",
                "stay_key",
                "event_id",
                "source_revision",
            )
        },
        "job_key": key,
        "reference_id": "tg39-" + hashlib.sha256(key.encode()).hexdigest(),
        "purpose": purpose,
        "sequence": sequence,
        "listing_map_id": booking["listing_map_id"],
        "language": booking["guest_language"],
        "phone_hash": booking["phone_hash"],
    }


def plan(packet, purpose, sequence, due, expiry):
    expected = identity(packet, purpose, sequence)
    booking = {
        k: v
        for k, v in packet["booking"].items()
        if k
        not in {
            "guest_name",
            "guest_first_name",
            "guest_last_name",
            "guest_email",
            "guest_phone_e164",
        }
    }
    row, _ = workgroup.message_model().objects.get_or_create(
        job_key=expected["job_key"],
        defaults={
            "scenario": packet["scenario"],
            "stay_id": packet["source_key"],
            "event_id": packet["event_id"],
            "revision": packet["source_revision"],
            "purpose": purpose,
            "sequence": sequence,
            "reference_id": expected["reference_id"],
            "identity": expected,
            "booking_basis": {
                "booking": booking,
                "source_event_kind": packet["source_event_kind"],
                "ownership_reference": packet["ownership_reference"],
            },
            "not_before": due,
            "expires_at": expiry,
        },
    )
    if row.identity != expected or row.not_before != due or row.expires_at != expiry:
        raise Denied
    return row


def checked_evidence(row, proof, attempt=None):
    if not isinstance(proof, dict) or proof.get("available") is not True:
        return None
    if (
        proof.get("contract")
        != (
            "tg39_workgroup_handoff_v1"
            if workgroup.is_handoff(row)
            else "tg39_stay_delivery_status_v1"
        )
        or proof.get("channel") != ("workgroup_manual" if workgroup.is_handoff(row) else "telegram")
        or proof.get("delivered") is not False
        or proof.get("read") is not False
        or not row.content_sha256
        or proof.get("content_sha256") != row.content_sha256
        or proof.get("phone_hash") != row.identity["phone_hash"]
        or proof.get(
            "workgroup_binding_fingerprint"
            if workgroup.is_handoff(row)
            else "chat_binding_fingerprint"
        )
        != row.binding_fingerprint
        or not row.binding_fingerprint
        or any(
            type(proof.get(k)) is not type(row.identity[k]) or proof[k] != row.identity[k]
            for k in (
                "job_key",
                "reference_id",
                "source_revision",
                "event_id",
                "purpose",
                "sequence",
                "stay_key",
                "source_owner",
                "source_key",
                "hostaway_reservation_id",
            )
        )
    ):
        raise Denied
    if workgroup.is_handoff(row) and (
        proof.get("workgroup_id") != workgroup.WORKGROUP_ID
        or proof.get("guest_accepted") is not False
        or row.binding_fingerprint != workgroup.fingerprint()
    ):
        raise Denied
    aid = proof.get("attempt_id")
    if not isinstance(aid, str) or str(uuid.UUID(aid)) != aid or attempt and aid != attempt:
        raise Denied
    if row.attempt_id and str(row.attempt_id) != aid:
        raise Denied
    if proof.get("status") == workgroup.completed_state(row) and proof.get("api_accepted") is True:
        message_id = proof.get("provider_message_id")
        if workgroup.is_handoff(row):
            ids = proof.get("provider_message_ids")
            if (
                not isinstance(ids, list)
                or not 1 <= len(ids) <= 64
                or any(
                    not isinstance(x, str) or not re.fullmatch(r"[1-9][0-9]{0,19}", x) for x in ids
                )
                or ids[-1] != str(message_id)
            ):
                raise Denied
        at = parse_datetime(proof.get("accepted_at", ""))
        if (
            not isinstance(message_id, str | int)
            or isinstance(message_id, bool)
            or not re.fullmatch(r"[1-9][0-9]{0,19}", str(message_id))
            or not at
            or not timezone.is_aware(at)
            or not row.created_at <= at <= timezone.now() + timedelta(seconds=5)
        ):
            raise Denied
        return {
            "state": workgroup.completed_state(row),
            "attempt_id": aid,
            "provider_message_id": str(message_id),
            "accepted_at": at,
        }
    if proof.get("status") in {"posting", "unknown"} and proof.get("api_accepted") is False:
        return {"state": "uncertain", "attempt_id": aid}
    raise Denied


def import_evidence(row, proof, attempt=None):
    value = checked_evidence(row, proof, attempt)
    if value is None:
        return False
    from apps.loyalty import invitation_registry as registry

    with (
        registry.invitation_lock() if row.purpose == "loyalty_invitation" else nullcontext(),
        transaction.atomic(),
    ):
        current = type(row).objects.select_for_update().get(pk=row.pk)
        checked_evidence(current, proof, attempt)
        if current.state == workgroup.completed_state(row):
            if (
                value["state"] != workgroup.completed_state(row)
                or current.provider_message_id != value["provider_message_id"]
                or workgroup.is_handoff(row)
                and current.booking_basis.get("workgroup_message_ids")
                != proof["provider_message_ids"]
            ):
                raise Denied
            return True
        for field, val in value.items():
            setattr(current, field, val)
        if workgroup.is_handoff(row) and value["state"] == "handed_off":
            current.booking_basis = {
                **current.booking_basis,
                "workgroup_message_ids": proof["provider_message_ids"],
            }
            value["booking_basis"] = current.booking_basis
        current.save(update_fields=[*value, "updated_at"])
        if current.purpose == "loyalty_invitation" and value["state"] == workgroup.completed_state(
            row
        ):
            from apps.loyalty import invitation_registry as registry
            from apps.loyalty.models import JoinInvitation

            invitation = JoinInvitation.objects.get(
                pk=current.booking_basis["loyalty_invitation_id"]
            )
            if invitation.channel != (
                "workgroup_manual" if workgroup.is_handoff(row) else "telegram"
            ):
                raise Denied
            registry.record(
                invitation, workgroup.completed_state(row), value["provider_message_id"]
            )
    return value["state"] == workgroup.completed_state(row)


def outstanding(scenario, stay_id):
    """Historical evidence first, independent of current admission/end/phone."""
    channel = TelegramStayChannel.objects.filter(scenario=scenario, stay_id=stay_id).first()
    if channel is None:
        return None
    rows = (
        workgroup.message_model()
        .objects.filter(
            scenario=scenario,
            stay_id=stay_id,
            state__in=["planned", "uncertain", workgroup.completed_state()],
        )
        .filter(Q(booking_basis__ack_seen__isnull=True) | Q(booking_basis__ack_seen=False))
        .exclude(content_sha256="")
    )
    if channel.receipt_cursor:
        rows = rows.filter(pk__gt=channel.receipt_cursor)
    page = list(rows.order_by("pk")[:9])
    for row in page[:8]:
        proof = bridge.read(
            row,
            "handoff_status" if workgroup.is_handoff(row) else "delivery_status",
            attempt=str(row.attempt_id) if row.attempt_id else None,
        )
        value = checked_evidence(row, proof)
        if value and value["state"] == workgroup.completed_state(row):
            recorded = proof.get("recorded_at")
            if recorded is not None:
                at = parse_datetime(recorded)
                if (
                    not at
                    or not timezone.is_aware(at)
                    or at < value["accepted_at"]
                    or at > timezone.now() + timedelta(seconds=5)
                ):
                    raise Denied
                import_evidence(row, proof)
                row.refresh_from_db()
                row.booking_basis = {**row.booking_basis, "ack_seen": True}
                row.save(update_fields=["booking_basis", "updated_at"])
                continue
            return bridge.packet_for(row, attempt=value["attempt_id"])
        if value:
            import_evidence(row, proof)
            raise Denied("prior_post_unconfirmed_no_retry")
    channel.receipt_cursor = page[7].pk if len(page) > 8 else None
    channel.save(update_fields=["receipt_cursor"])
    if len(page) > 8:
        raise ReceiptPagePending
    return None


def binding(row):
    if workgroup.is_handoff(row):
        return workgroup.fingerprint()
    proof = bridge.read(row, "binding")
    chat = proof.get("chat_id")
    if (
        proof.get("available") is not True
        or proof.get("revoked") is not False
        or not isinstance(chat, str)
        or not re.fullmatch(r"[1-9][0-9]{0,15}", chat)
        or proof.get("user_id") != chat
        or proof.get("phone_hash") != row.identity["phone_hash"]
    ):
        raise Denied("guest_binding_required")
    return render.sha(row.identity["stay_key"] + ":" + chat + ":" + row.identity["phone_hash"])


def asset_for(scenario, purpose, language, *, verified_paid=False):
    if scenario != "07" or purpose != "status_new":
        return None
    from .morning_payment_messages import notice_image

    raw = notice_image(language, confirmed=verified_paid)
    # notice_image is the existing approved static source; no new public asset.
    return {
        "asset_ref": f"site01-morning-{'confirmed' if verified_paid else 'payment'}-{language}",
        "asset_sha256": hashlib.sha256(raw).hexdigest(),
        "asset_mime": "image/png",
    }


@sensitive_variables()
def core_message(row, packet, stay):
    context = bridge.read(row, "render_source")
    template, variables = render.verified_context(context, row.identity)
    zone = ZoneInfo(stay.time_zone)
    start, end = stay.starts_at.astimezone(zone), stay.ends_at.astimezone(zone)
    variables.update(
        guest_name=stay.guest_name,
        listing_name=getattr(stay.property, "name_" + row.identity["language"], "")
        or stay.property.name_en,
        arrival_date=start.date().isoformat(),
        departure_date=end.date().isoformat(),
        check_in_time=start.strftime("%H:%M"),
        check_out_time=end.strftime("%H:%M"),
        timezone=stay.time_zone,
    )
    body = render.render_html(template, variables, row.identity["language"])
    verified_paid = False
    if row.scenario == "07" and row.purpose == "status_new":
        from .morning07_payments import verified_facts

        current = type(stay).objects.select_related("property").get(pk=stay.pk)
        if current.communication_revision != row.revision:
            raise Denied
        verified_paid = bool(verified_facts(current))
        if row.asset_ref:
            language = row.identity["language"]
            if row.asset_ref not in {
                f"site01-morning-confirmed-{language}",
                f"site01-morning-payment-{language}",
            }:
                raise Denied
            if row.asset_ref == f"site01-morning-confirmed-{language}" and not verified_paid:
                raise Denied("confirmation_image_payment_proof_missing")
            # An unpaid image remains truthful after payment. Never change a
            # planned job's media/hash merely because collection later succeeds.
            verified_paid = row.asset_ref == f"site01-morning-confirmed-{language}"
    return render.message(
        row.identity["language"],
        body,
        asset_for(row.scenario, row.purpose, row.identity["language"], verified_paid=verified_paid),
        manual_handoff=workgroup.is_handoff(row),
    ), context["context_sha256"]


def accepted_core(stay, scenario, *, fingerprint=None):
    if workgroup.enabled():
        return False
    return core_complete(stay, scenario, fingerprint=fingerprint)


def core_handed_off(stay, scenario, *, fingerprint=None):
    return workgroup.enabled() and core_complete(stay, scenario, fingerprint=fingerprint)


def core_complete(stay, scenario, *, fingerprint=None):
    rows = list(
        workgroup.message_model()
        .objects.filter(
            scenario=scenario,
            stay_id=stay.pk,
            revision=stay.communication_revision,
            sequence__in=[1, 2, 3],
            state=workgroup.completed_state(),
            recipient_kind="workgroup" if workgroup.enabled() else "guest",
        )
        .order_by("sequence")
    )
    phone_hash = render.sha(workgroup.canonical_phone(stay.guest_phone).lstrip("+"))
    return bool(
        len(rows) == 3
        and [r.sequence for r in rows] == [1, 2, 3]
        and rows[0].purpose in {"status_new", "status_modified"}
        and [r.purpose for r in rows[1:]] == ["house_rules", "checkin_guide"]
        and len({r.event_id for r in rows}) == 1
        and all(
            r.identity["phone_hash"] == phone_hash
            and r.content_sha256
            and r.attempt_id
            and r.accepted_at
            and r.provider_message_id
            and r.binding_fingerprint
            for r in rows
        )
        and len({r.binding_fingerprint for r in rows}) == 1
        and all(rows[n].accepted_at <= rows[n + 1].accepted_at for n in (0, 1))
        and (fingerprint is None or rows[0].binding_fingerprint == fingerprint)
    )


def next_packet(packet, data):
    from .telegram_stay_source import resolve

    if not TelegramStayChannel.objects.filter(
        scenario=packet["scenario"], stay_id=packet["source_key"]
    ).exists():
        return {**packet, "decision": "hold", "reason": "telegram_channel_not_selected"}
    with transaction.atomic():
        scenario, stay, event, _, _, financial = resolve(data)
    if data["operation"] == "review_next":
        return review_packet(packet, stay, event)
    if financial:
        return financial_packet(packet, stay, event)
    kind = event.payload["event_type"]
    purposes = (
        ["status_cancelled"]
        if kind == "cancelled"
        else [
            "status_new" if kind == "new" else "status_modified",
            "house_rules",
            "checkin_guide",
        ]
    )
    for sequence, purpose in enumerate(purposes, 1):
        due = event.created_at
        row = plan(packet, purpose, sequence, due, stay.ends_at)
        if row.state == workgroup.completed_state(row):
            continue
        if row.state == "uncertain":
            return {**packet, "decision": "hold", "reason": "prior_post_unconfirmed_no_retry"}
        return send_packet(row, packet, stay)
    if kind == "cancelled":
        return {**packet, "decision": "done", "reason": "cancellation_accepted"}
    due = stay.ends_at - timedelta(hours=1)
    payment = payment_link_packet(packet, stay, event, next_check=due)
    if payment["decision"] not in {"done", "wait"} or payment.get("reason") not in {
        "awaiting_payment",
        "paid_journey_accepted",
        "loyalty_member",
        "loyalty_duplicate",
    }:
        return payment
    if due > timezone.now():
        return {**payment, "decision": "wait", "next_check_at": due.isoformat()}
    row = plan(packet, "reminder_checkout", 4, due, stay.ends_at)
    if row.state == workgroup.completed_state(row):
        return payment
    return send_packet(row, packet, stay)


def send_packet(row, packet, stay):
    if row.state in {"uncertain", workgroup.completed_state(row)}:
        raise Denied("prior_attempt_not_sendable")
    try:
        fingerprint = binding(row)
    except Denied:
        return {**packet, "decision": "binding_required", "reason": "guest_binding_required"}
    if row.purpose in {"payment_link", "payment_success", "loyalty_invitation"}:
        from .telegram_stay_financial import content as financial_content

        content, source_sha = financial_content(row, stay)
    elif row.purpose.startswith("review_"):
        from .telegram_stay_reviews import content as review_content

        content, source_sha = review_content(row, stay)
    else:
        content, source_sha = core_message(row, packet, stay)
    with transaction.atomic():
        current = type(row).objects.select_for_update().get(pk=row.pk)
        if current.content_sha256 and (
            current.content_sha256 != content["content_sha256"]
            or current.binding_fingerprint != fingerprint
        ):
            raise Denied("immutable_message_conflict")
        current.content_sha256 = content["content_sha256"]
        current.binding_fingerprint = fingerprint
        current.render_sha256 = source_sha
        current.asset_ref = content.get("asset_ref", "")
        current.asset_sha256 = content.get("asset_sha256", "")
        if row.purpose == "payment_link":
            from .telegram_stay_financial import modules

            current.booking_basis = {
                **current.booking_basis,
                "payment_fingerprint": modules(row.scenario)[0].fingerprint(stay),
            }
        current.save(
            update_fields=[
                "content_sha256",
                "binding_fingerprint",
                "render_sha256",
                "asset_ref",
                "asset_sha256",
                "booking_basis",
                "updated_at",
            ]
        )
    return {
        **packet,
        **workgroup.packet_fields(),
        "decision": "send",
        "reason": "current_source_job",
        "job": {
            **content,
            "job_key": row.job_key,
            "reference_id": row.reference_id,
            "source_revision": row.revision,
            "purpose": row.purpose,
            "sequence": row.sequence,
            "recipient_kind": row.recipient_kind,
            "due_at": row.not_before.isoformat(),
            "expires_at": row.expires_at.isoformat(),
            "source_capability": bridge.record_capability(current),
        },
    }


def payment_link_packet(packet, stay, event, *, next_check=None):
    from .telegram_stay_financial import payment_link

    return payment_link(packet, stay, event, next_check)


def financial_packet(packet, stay, event):
    from .telegram_stay_financial import financial

    return financial(packet, stay, event)


def review_packet(packet, stay, event):
    from .telegram_stay_reviews import next_review

    return next_review(packet, stay, event)


@private_endpoint
@authenticated("POST")
@sensitive_variables()
def record(request):
    if not getattr(settings, "TELEGRAM_STAY_RECEIPTS_ENABLED", False):
        return private_response({"error": "stay_receipts_closed"}, 404)
    try:
        if request.GET or len(request.body) > 4096:
            raise Denied
        data = json.loads(request.body, object_pairs_hook=duplicate_safe)
        request._body = b""
        if (
            not isinstance(data, dict)
            or set(data)
            != {
                "contract",
                "scenario",
                "stay_key",
                "source_revision",
                "job_key",
                "reference_id",
                "attempt_id",
                "source_capability",
            }
            or data["contract"]
            != (
                "tg39_workgroup_record_request_v1"
                if workgroup.enabled()
                else "tg39_stay_record_request_v1"
            )
        ):
            raise Denied
        row = bridge.record_row(data["source_capability"])
        if any(
            type(data[k]) is not type(row.identity[k]) or data[k] != row.identity[k]
            for k in ("scenario", "stay_key", "source_revision", "job_key", "reference_id")
        ):
            raise Denied
        proof = bridge.read(
            row,
            "handoff_status" if workgroup.is_handoff(row) else "delivery_status",
            attempt=data["attempt_id"],
        )
        accepted = import_evidence(row, proof, data["attempt_id"])
        return private_response(
            {
                "contract": "tg39_workgroup_record_result_v1"
                if workgroup.is_handoff(row)
                else "tg39_stay_record_result_v1",
                "recorded": accepted,
                "job_key": row.job_key,
                "attempt_id": data["attempt_id"],
                "decision": "continue" if accepted else "hold",
            },
            200 if accepted else 409,
        )
    except (
        Denied,
        Unavailable,
        signing.BadSignature,
        ValueError,
        TypeError,
        KeyError,
        TelegramStayMessage.DoesNotExist,
        workgroup.message_model().DoesNotExist,
    ):
        return private_response({"error": "stay_receipt_unverified"}, 409)
