"""04 owns review timing and completion; the website supplies current stay/payment proof."""

import re
from datetime import timedelta
from urllib.parse import urlsplit

from django.utils import timezone
from django.utils.dateparse import parse_datetime

from . import message_source_authorization as source
from . import telegram_stay_authority as bridge
from . import telegram_stay_render as render
from . import telegram_workgroup as workgroup
from .telegram_authority import Denied

PURPOSES = {1: "review_request", 2: "review_reminder_1", 3: "review_reminder_2"}


def checked(policy, stay):
    if (
        not isinstance(policy, dict)
        or policy.get("contract") != "tg39_stay_review_policy_v1"
        or policy.get("available") is not True
    ):
        raise Denied("review_policy_not_bound")
    if policy.get("decision") != "send":
        return None
    step = policy.get("step")
    body = policy.get("template_body")
    due, expiry = (
        parse_datetime(policy.get("not_before", "")),
        parse_datetime(policy.get("expires_at", "")),
    )
    evidence = policy.get("profile_evidence", {})
    url = urlsplit(policy.get("review_url", ""))
    if (
        type(step) is not int
        or step not in PURPOSES
        or policy.get("purpose") != PURPOSES[step]
        or policy.get("template_key") != f"website_review_{step}_{stay.guest_language}"
        or not isinstance(body, str)
        or render.sha(body) != policy.get("template_sha256")
        or not due
        or not expiry
        or not timezone.is_aware(due)
        or not timezone.is_aware(expiry)
        or not stay.ends_at <= due <= timezone.now() < expiry <= stay.ends_at + timedelta(days=3)
        or url.scheme != "https"
        or url.hostname != "g.page"
        or url.username
        or url.password
        or not policy.get("google_profile_id")
        or evidence.get("kind") != "owner_maintained_approved_mapping"
        or evidence.get("approval_at") is not None
        or not evidence.get("source")
        or not re.fullmatch(r"[a-f0-9]{64}", str(evidence.get("row_sha256", "")))
    ):
        raise Denied("review_policy_unverified")
    return due, expiry


def next_review(packet, stay, event):
    from .telegram_stay_journey import plan, send_packet

    if stay.status == "cancelled" or not source.SOURCES[packet["scenario"]][5].verified_facts(
        stay, allow_ended=True
    ):
        return {**packet, "decision": "hold", "reason": "review_current_paid_booking_required"}
    probe = plan(packet, "review_policy", 0, event.created_at, stay.ends_at + timedelta(days=3))
    policy = bridge.read(probe, "review_policy")
    window = checked(policy, stay)
    if window is None:
        return {
            **packet,
            "decision": policy.get("decision", "hold"),
            "reason": policy.get("reason", "review_not_due"),
            **({"next_check_at": policy["not_before"]} if policy.get("not_before") else {}),
        }
    row = plan(packet, policy["purpose"], 10 + policy["step"], *window)
    if row.state == workgroup.completed_state(row):
        return {**packet, "decision": "wait", "reason": "review_step_already_accepted"}
    return send_packet(row, packet, stay)


def content(row, stay):
    policy = bridge.read(row, "review_policy")
    if (
        checked(policy, stay) != (row.not_before, row.expires_at)
        or policy["purpose"] != row.purpose
    ):
        raise Denied("review_policy_changed")
    template = policy["template_body"]
    template = re.sub(r"\*([^*\n]+)\*", r"<b>\1</b>", template)
    template = re.sub(r"(?<!\{)\{(guest_name|listing_name|review_url)\}(?!\})", r"{{\1}}", template)
    values = {
        "guest_name": stay.guest_name,
        "listing_name": getattr(stay.property, "name_" + stay.guest_language, "")
        or stay.property.name_en,
        "review_url": policy["review_url"],
    }
    return render.message(
        stay.guest_language,
        render.render_html(template, values, stay.guest_language),
        manual_handoff=workgroup.is_handoff(row),
    ), policy["template_sha256"]
