import json
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pytest

from apps.reservations import message_source_authorization as source
from apps.reservations.website_message_policy import policy_from_source
from apps.reservations.website_source_renderer import render_trusted_website_source

FIXTURES = json.loads(
    (Path(__file__).parent / "fixtures/website-render-source.json").read_text(encoding="utf-8")
)["fixtures"]


@pytest.mark.parametrize("fixture", FIXTURES)
def test_exact_owner20_b02_and_js_digest_parity(fixture):
    assert (
        render_trusted_website_source(fixture["context"], fixture["expected_renderer_sha"])
        == fixture["expected"]
    )


def synthetic_context():
    context = deepcopy(next(f["context"] for f in FIXTURES if f["expected"].get("ok") is True))
    facts = {
        key: context[key]
        for key in (
            "scenario",
            "job_sha256",
            "reservation_key",
            "reservation_revision",
            "source_event_key",
            "source_message_type",
            "purpose",
            "not_before",
            "expires_at",
        )
    }
    facts["recipient_e164"] = "+16502530000"
    context["recipient_binding_sha256"] = source.sha(
        f"{facts['reservation_key']}:{facts['recipient_e164']}"
    )
    event = SimpleNamespace(stay=SimpleNamespace(guest_language=context["language"]))
    return event, facts, context


def test_independent_bound_source_can_reconstruct_but_never_approve_provider():
    event, facts, context = synthetic_context()
    policy = policy_from_source(event, facts, context)
    assert policy and policy["render_policy_ref"].startswith("yc20-web-render-v1:")
    assert set(policy) == {"language", "message", "render_policy_ref"}


@pytest.mark.parametrize(
    "field,value",
    [
        ("scenario", "99"),
        ("reservation_revision", 1),
        ("source_event_key", "website:07:core:other"),
        ("purpose", "payment_confirmation"),
        ("source_message_type", "payment_success"),
        ("recipient_binding_sha256", "f" * 64),
        ("renderer_sha256", "f" * 64),
        ("source_template_sha256", "f" * 64),
        ("stored_body_sha256", "f" * 64),
        ("language", "unknown"),
        ("not_before", "2026-01-01T00:00:00"),
        ("expires_at", "2026-01-01T00:00:00Z"),
        ("media_required", True),
    ],
)
def test_context_must_match_own_source_and_pinned_renderer(field, value):
    event, facts, context = synthetic_context()
    context[field] = value
    assert policy_from_source(event, facts, context) is None
