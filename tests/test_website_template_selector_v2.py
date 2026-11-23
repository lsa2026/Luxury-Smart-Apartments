import hashlib
import json
from pathlib import Path

import pytest
from pypdf import PdfReader

from apps.reservations import website_template_selector_v2 as selector
from apps.reservations.access_guide_pdf_v2 import render_access_guide
from apps.reservations.website_template_contract_v2 import content_digest

ROOT = Path(__file__).parent / "fixtures/source-v2"
CASES = json.loads((ROOT / "selector-parity.json").read_text(encoding="utf8"))["cases"]
PDF = json.loads((ROOT / "pdf-parity.json").read_text(encoding="utf8"))


@pytest.mark.parametrize("case", CASES, ids=lambda c: c["name"] + "-" + c["language"])
def test_source01_matches_frozen_backend_selector_and_private_bytes(case, monkeypatch):
    monkeypatch.setattr("httpx.Client.request", lambda *a, **k: pytest.fail("network forbidden"))
    value = selector.select_source_message(case["facts"], case["context"])
    assert value.message == case["expected_message"]
    assert (
        value.content_digest
        == case["expected_digest"]
        == content_digest(case["language"], value.message)
    )
    if value.attachment_bytes:
        assert (
            hashlib.sha256(value.attachment_bytes).hexdigest() == case["expected_attachment_sha256"]
        )
        assert PDF["values"]["gate_code"] not in str(value.message)
        assert PDF["values"]["wifi_pass"] not in str(value.message)
    else:
        assert case["expected_attachment_sha256"] is None


@pytest.mark.parametrize("language", ["ar", "en", "fr"])
def test_shared_renderer_is_byte_identical_and_all_fourteen_values_required(language):
    from io import BytesIO

    raw = render_access_guide(PDF["values"], language)
    assert hashlib.sha256(raw).hexdigest() == PDF["expected_sha256"][language]
    reader = PdfReader(BytesIO(raw))
    assert len(reader.pages) == 1
    links = [a.get_object().get("/A", {}).get("/URI") for a in reader.pages[0]["/Annots"]]
    assert sorted(links) == sorted([PDF["values"]["map_url"], PDF["values"]["video_url"]])
    for field in PDF["values"]:
        with pytest.raises(ValueError):
            render_access_guide({**PDF["values"], field: ""}, language)


def test_source_image_is_checked_not_dropped_and_new_confirmation_never_claims_payment():
    from apps.reservations.morning_payment_messages import notice_image

    case = next(
        c
        for c in CASES
        if c["name"] == "lsa_morning_booking_confirmed_v2" and c["language"] == "ar"
    )
    value = selector.select_source_message(case["facts"], case["context"])
    assert value.message["attachment"]["sha256"] == hashlib.sha256(notice_image("ar")).hexdigest()
    with pytest.raises(selector.V2Hold):
        selector.select_source_message(
            case["facts"], case["context"], asset_reader=lambda *_: b"wrong"
        )


GATE_CASES = json.loads((ROOT / "gate-access-parity.json").read_text(encoding="utf8"))["cases"]


@pytest.mark.parametrize("case", GATE_CASES, ids=lambda c: c["language"])
def test_verified_instruction_matches_source19_private_bytes_and_identity_digest(case):
    from copy import deepcopy

    current = selector.select_source_message(case["facts"], case["context"])
    assert current.message == case["expected_message"]
    assert current.content_digest == case["expected_digest"]
    assert (
        hashlib.sha256(current.attachment_bytes).hexdigest() == case["expected_attachment_sha256"]
    )
    changed = deepcopy(case["context"])
    changed["gate_access_evidence_ref"] += ":changed"
    another = selector.select_source_message(case["facts"], changed)
    assert another.attachment_bytes == current.attachment_bytes
    assert another.content_digest != current.content_digest
    assert (
        another.message["attachment"]["binding_sha256"]
        != current.message["attachment"]["binding_sha256"]
    )
    del changed["gate_access_evidence_ref"]
    with pytest.raises(selector.V2Hold, match="source_gate_access_not_verified"):
        selector.select_source_message(case["facts"], changed)


@pytest.mark.parametrize("language", ["ar", "en", "fr"])
def test_real_gate_code_wins_and_old_pdf_bytes_stay_identical(language):
    values = {**PDF["values"], "gate_access_instruction": "SYNTHETIC IGNORED INSTRUCTION"}
    assert (
        hashlib.sha256(render_access_guide(values, language)).hexdigest()
        == PDF["expected_sha256"][language]
    )
