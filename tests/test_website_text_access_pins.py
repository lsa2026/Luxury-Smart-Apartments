"""Compare local pinned templates with the independent provider metadata capture."""

import hashlib
import json
from pathlib import Path

import pytest

from apps.reservations import website_template_selector_v2 as selector
from apps.reservations import website_text_access_v1 as profile

CAPTURE = Path(__file__).parent / "fixtures/source-v2/text-access-provider-pins.json"
PARITY = json.loads(
    (Path(__file__).parent / "fixtures/source-v2/text-access-selector-parity.json").read_text(
        encoding="utf-8"
    )
)["fixtures"]


def test_source_registry_has_exact_six_provider_approved_body_components():
    raw = CAPTURE.read_bytes().replace(b"\r\n", b"\n")
    assert (
        hashlib.sha256(raw).hexdigest()
        == "00ceb7048329c269d55a9e08490b9b242a009673d89e48c9f5d5787ebc6ee35c"
    )
    captured = json.loads(raw)
    registry = profile.registry()
    assert captured["actual_provider_metadata_verified"] is True
    assert len(captured["snapshots"]) == 6
    for expected in captured["snapshots"]:
        actual = [
            row
            for row in registry["snapshots"]
            if (row["name"], row["language"]) == (expected["name"], expected["language"])
        ]
        assert len(actual) == 1
        row = actual[0]
        assert row["waba_id"] == captured["waba_id"]
        assert row["status"] == "APPROVED" and row["category"] == "UTILITY"
        assert row["components"] == expected["components"]
        assert (
            row["components_sha256"]
            == expected["components_sha256"]
            == selector.digest2(row["components"])
        )
        assert [component["type"] for component in row["components"]] == ["BODY"]


def test_source_bindings_keep_distinct_order_and_no_document_for_text_access():
    registry = profile.registry()
    expected = {
        "checkin_guide": (
            "lsa_sameday_checkin_guide_v1",
            [
                "guest_name",
                "listing_name",
                "map_url",
                "property_address",
                "wifi_net",
                "wifi_pass",
                "gate_code",
                "video_url",
            ],
        ),
        "checkin_reminder": (
            "lsa_checkin_reminder_v1",
            [
                "guest_name",
                "listing_name",
                "property_address",
                "map_url",
                "wifi_net",
                "wifi_pass",
                "video_url",
                "gate_code",
            ],
        ),
    }
    for name, keys in expected.values():
        assert registry["ordered_source_keys"][name] == keys
        assert {r["language"] for r in registry["snapshots"] if r["name"] == name} == {
            "ar",
            "en",
            "fr",
        }
    bindings, _ = selector.manifests()
    assert all(
        r.get("asset", {}).get("kind") == "document"
        for r in bindings["templates"]
        if r["purpose"] in expected
    )


@pytest.mark.parametrize("case", PARITY, ids=lambda case: case["id"])
def test_independent_selector_matches_frozen_backend_text_results(case, monkeypatch):
    monkeypatch.setattr("httpx.Client.request", lambda *_a, **_k: pytest.fail("network forbidden"))
    selected = selector.select_source_message(
        case["facts"],
        case["context"],
        text_access=True,
        now=case["now_epoch"],
        retained_message=case.get("retained_message"),
        retained_at=case.get("retained_at"),
    )
    assert selected.message == case["message"]
    assert selected.message["attachment"] is None and selected.attachment_bytes is None
    assert selected.content_digest == case["content_digest"]
    assert selected.selection_ref == case["selection_ref"]
    assert selected.source_values_sha256 == case["source_values_sha256"]
    assert profile.selection_identity(selected.message) == (
        case["selection_ref"],
        profile.registry()["override_sha256"],
    )


def test_unknown_or_wrong_profile_pair_cannot_choose_a_text_renderer():
    sha = profile.registry()["override_sha256"]
    assert not profile.requested_profile(selector.SELECTION_REF, selector.PACK_SHA)
    assert profile.requested_profile("yc19-text-access-render-1:" + sha, sha)
    for reference, pack in [
        ("yc19-text-access-render-1:" + "e" * 64, sha),
        ("yc19-text-access-render-1:" + sha, "e" * 64),
    ]:
        with pytest.raises(ValueError, match="text_access_profile_mismatch"):
            profile.requested_profile(reference, pack)


def test_profile_bytes_are_pinned_even_if_the_file_has_its_own_checksum(monkeypatch):
    original = Path.read_bytes

    def changed(path):
        return b"{}" if path.name == "text-access-profile-v1.json" else original(path)

    monkeypatch.setattr(Path, "read_bytes", changed)
    with pytest.raises(ValueError, match="text_access_registry_changed"):
        profile.registry()
