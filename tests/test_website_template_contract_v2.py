from copy import deepcopy

import pytest

from apps.reservations import message_receipts as receipts
from apps.reservations import website_template_contract_v2 as contract


def message():
    return {
        "mode": "template",
        "template_ref": "lsa_stay_arrival_document_v2",
        "variables": ["Synthetic Guest", "Synthetic Apartment", "AM-example", "2026-12-04"],
        "components_sha256": "a" * 64,
        "attachment": {
            "kind": "document",
            "asset_ref": "document:" + "b" * 64,
            "sha256": "b" * 64,
            "media_type": "application/pdf",
            "size_bytes": 75000,
            "filename": "arrival-guide-en.pdf",
            "expires_at": "2026-12-04T12:00:00+03:00",
            "binding_sha256": "c" * 64,
        },
    }


def test_v2_digest_binds_all_content_and_has_order_independent_keys():
    original = message()
    digest = receipts.content_digest("en", original)
    assert digest == contract.content_digest("en", dict(reversed(list(original.items()))))
    for path, value in [
        (("variables", 0), "Other guest"),
        (("components_sha256",), "d" * 64),
        (("attachment", "binding_sha256"), "d" * 64),
        (("attachment", "expires_at"), "2026-12-04T13:00:00+03:00"),
        (("attachment", "size_bytes"), 75001),
    ]:
        changed = deepcopy(original)
        target = changed
        for key in path[:-1]:
            target = target[key]
        target[path[-1]] = value
        assert receipts.content_digest("en", changed) != digest


@pytest.mark.parametrize(
    "change", ["url", "extra", "nozone", "wrongtype", "oversize", "filename", "hash", "mime"]
)
def test_untrusted_media_descriptor_cannot_be_laundered(change):
    value = message()
    asset = value["attachment"]
    if change == "url":
        asset["asset_ref"] = "https://example.invalid/sample.pdf"
    elif change == "extra":
        asset["provider_media_id"] = "not-source-proof"
    elif change == "nozone":
        asset["expires_at"] = "2026-12-04T12:00:00"
    elif change == "wrongtype":
        asset["size_bytes"] = True
    elif change == "oversize":
        asset["size_bytes"] = 2000001
    elif change == "filename":
        asset["filename"] = "guest-identifier.pdf"
    elif change == "hash":
        asset["sha256"] = "B" * 64
    else:
        asset["media_type"] = "image/png"
    with pytest.raises(ValueError):
        receipts.content_digest("en", value)


def test_v1_digest_and_template_shape_are_unchanged():
    original = {"mode": "template", "template_ref": "existing_v1", "variables": ["قيمة"]}
    expected = receipts.source.sha(
        '["ar",{"mode":"template","template_ref":"existing_v1","variables":["قيمة"]}]'
    )
    assert receipts.content_digest("ar", original) == expected
    assert receipts.content_digest(
        "en", {"mode": "text", "text": "Existing text"}
    ) == receipts.source.sha('["en",{"mode":"text","text":"Existing text"}]')


def test_binding_is_recipient_revision_source_values_and_bytes_specific():
    fields = (
        "source_family",
        "scenario",
        "job_sha256",
        "reservation_key",
        "reservation_revision",
        "recipient_ref",
        "purpose",
        "language",
        "source_event_key",
    )
    facts = dict.fromkeys(fields, "synthetic")
    values = {"gate_code": "NOT_A_REAL_CODE", "wifi_pass": "NOT_A_REAL_PASSWORD"}
    digest = contract.attachment_binding(facts, values, "a" * 64)
    for key in fields:
        assert contract.attachment_binding({**facts, key: "changed"}, values, "a" * 64) != digest
    assert (
        contract.attachment_binding(facts, {**values, "gate_code": "OTHER_SAMPLE"}, "a" * 64)
        != digest
    )
    assert contract.attachment_binding(facts, values, "b" * 64) != digest
