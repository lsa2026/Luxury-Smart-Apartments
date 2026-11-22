"""Fixed approved source copy and synthetic private context; no network or guest data."""

import json
from datetime import timedelta
from pathlib import Path

import pytest
from django.utils import timezone

from apps.reservations import telegram_stay_render as render
from apps.reservations.telegram_authority import Denied

TEMPLATES = json.loads(
    (Path(__file__).parent / "fixtures/telegram_stay_templates.json").read_text()
)


@pytest.mark.parametrize("row", TEMPLATES["templates"])
@pytest.mark.parametrize("language", ["ar", "en", "fr"])
def test_all36_existing_source_templates_escape_guest_and_keep_approved_markup(row, language):
    variables = {
        "guest_name": "SYNTHETIC </b><script>value&</script> * _ 😀",
        "listing_name": "Synthetic <apartment>",
        "arrival_date": "2026-12-01",
        "departure_date": "2026-12-02",
        "check_in_time": "06:00",
        "check_out_time": "20:00",
        "timezone": "Asia/Riyadh",
        **dict.fromkeys(render.OPERATIONAL - {"timezone"}, "SYNTHETIC_ONLY"),
    }
    value = render.render_html(row["content_" + language], variables, language)
    assert "<script>" not in value and "{{" not in value
    if "{{guest_name}}" in row["content_" + language]:
        assert "&lt;/b&gt;&lt;script&gt;value&amp;&lt;/script&gt;" in value
    assert "<br>" not in value
    packet = render.message(language, value)
    assert packet["method"] == "text" and packet["parse_mode"] == "HTML"
    assert len(packet["content_sha256"]) == 64


def context():
    expected = dict(
        source_owner="morning07",
        source_key="11111111-1111-4111-8111-111111111111",
        hostaway_reservation_id="97007123",
        scenario="07",
        stay_key="morning07:synthetic",
        event_id="22222222-2222-4222-8222-222222222222",
        source_revision=1,
        job_key="tg39:synthetic",
        reference_id="tg39:ref:synthetic",
        purpose="house_rules",
        sequence=2,
        listing_map_id=97007,
        language="en",
        phone_hash="a" * 64,
    )
    variables = {"timezone": "Asia/Riyadh", "map_url": "https://example.invalid"}
    raw_variables = json.dumps(variables, ensure_ascii=False, sort_keys=True)
    template = "<b>Welcome {{guest_name}}</b><br>{{map_url}}"
    proof = {
        **expected,
        "source_template_name": "house_rules",
        "source_template_text": template,
        "source_template_sha256": render.sha(template),
        "template_version": "sha256:" + render.sha(template),
        "variables": variables,
        "variables_canonical_json": raw_variables,
        "variables_sha256": render.sha(raw_variables),
    }
    canonical = json.dumps({k: proof[k] for k in render.CANONICAL}, sort_keys=True)
    proof.update(
        contract="tg39_stay_render_source_v1",
        available=True,
        dispatch_allowed=False,
        context_canonical_json=canonical,
        context_sha256=render.sha(canonical),
        authoritative_at=timezone.now().isoformat(),
        expires_at=(timezone.now() + timedelta(seconds=25)).isoformat(),
    )
    return expected, proof


def test_postgres_canonical_json_is_hashed_as_received_without_reserialization():
    expected, proof = context()
    template, variables = render.verified_context(proof, expected)
    assert template == proof["source_template_text"] and variables == proof["variables"]


@pytest.mark.parametrize(
    "field,value",
    [
        ("source_revision", "1"),
        ("job_key", "foreign-job"),
        ("phone_hash", "b" * 64),
        ("source_template_text", "different"),
        ("variables_canonical_json", "{}"),
        ("context_sha256", "c" * 64),
        ("dispatch_allowed", True),
        ("available", False),
        ("expires_at", "2026-01-01T00:00:00Z"),
    ],
)
def test_context_identity_types_template_digests_and_window_cannot_be_substituted(field, value):
    expected, proof = context()
    proof[field] = value
    with pytest.raises(Denied):
        render.verified_context(proof, expected)


@pytest.mark.parametrize(
    "template", ["<script>x</script>", '<b class="x">x</b>', "<b>x", "{{missing}}"]
)
def test_unknown_markup_or_missing_source_variable_holds(template):
    with pytest.raises(Denied):
        render.render_html(template, {}, "en")


def test_content_digest_matches_native_javascript_utf8_tuple():
    assert render.message("ar", "<b>مرحبًا &amp; 😀</b>")["content_sha256"] == (
        "ccd4fcbee5c8b15c150823a918f5fa2992cbd4987c9bbb4e2c538d99ae66cd4c"
    )


def test_photo_keeps_asset_and_refuses_oversized_caption_without_dropping_content():
    asset = {
        "asset_ref": "site01-morning-confirmed-en",
        "asset_sha256": "a" * 64,
        "asset_mime": "image/png",
    }
    value = render.message("en", "<b>Test</b>", asset)
    assert value["method"] == "photo" and value["asset_sha256"] == asset["asset_sha256"]
    with pytest.raises(Denied, match="telegram_message_oversize"):
        render.message("en", "😀" * 513, asset)
