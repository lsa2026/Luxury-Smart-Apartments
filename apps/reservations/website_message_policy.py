"""Verify owner20's authenticated render source independently of planned content.

The context must come from the fixed backend's scoped source-reader endpoint,
never an n8n/API body. This proves content, not consent or Meta approval.
"""

from django.utils import timezone
from django.utils.crypto import constant_time_compare
from django.utils.dateparse import parse_datetime

from . import message_source_authorization as source
from .website_source_renderer import render_trusted_website_source

RENDERERS = {
    "07": "6e0fca4a439d2da888a0f5069b77787ab69c58eceb7388562c4a160bdef6bd12",
    "08": "73c3496d6696be6b1e59fcb93729de8b5dde64e87db52b5491a157bce7c1c84c",
}


def policy_from_source(event, facts, context):
    if not isinstance(context, dict) or ":core:" not in facts["source_event_key"]:
        return None
    for key in (
        "scenario",
        "job_sha256",
        "reservation_key",
        "reservation_revision",
        "source_event_key",
        "source_message_type",
        "purpose",
    ):
        if type(context.get(key)) is not type(facts[key]) or context[key] != facts[key]:
            return None
    if (
        context.get("source_family") != f"lsa{facts['scenario']}"
        or context.get("source_template_name") != facts["source_message_type"]
        or context.get("language") != event.stay.guest_language
    ):
        return None
    for key in ("not_before", "expires_at"):
        try:
            actual, expected = parse_datetime(context[key]), parse_datetime(facts[key])
        except (KeyError, ValueError, TypeError):
            return None
        if not actual or not timezone.is_aware(actual) or actual != expected:
            return None
    recipient = source.sha(f"{facts['reservation_key']}:{facts['recipient_e164']}")
    binding = context.get("recipient_binding_sha256")
    if not isinstance(binding, str) or not constant_time_compare(recipient, binding):
        return None
    rendered = render_trusted_website_source(context, RENDERERS[facts["scenario"]])
    if rendered.get("ok") is not True:
        return None
    return {
        "language": context["language"],
        "message": rendered["message"],
        "render_policy_ref": rendered["render_policy_ref"],
    }
