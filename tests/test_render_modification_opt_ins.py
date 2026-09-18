"""A Blueprint sync must not overwrite the operator's live modification opt-ins."""

import re
from pathlib import Path

import pytest


@pytest.mark.parametrize(
    "key",
    [
        "BOOKING_AUTOMATIC_MODIFICATION_APPROVAL",
        "HOSTAWAY_LIVE_MODIFICATION_ENABLED",
        "HOSTAWAY_LIVE_EXTENSION_ENABLED",
        "BOOKING_AUTOMATIC_CANCELLATION_ENABLED",
        "BOOKING_AUTOMATIC_REFUND_ENABLED",
        "HOSTAWAY_LIVE_CANCELLATION_ENABLED",
    ],
)
def test_render_preserves_live_modification_opt_ins(key):
    blueprint = (Path(__file__).resolve().parents[1] / "render.yaml").read_text()
    entries = re.findall(rf"^      - key: {key}\n((?:        [^\n]*\n)+)", blueprint, re.MULTILINE)
    assert len(entries) == 2, "Web and shared Celery configuration must both be covered"
    for entry in entries:
        assert re.search(r"^\s+sync: false\s*$", entry, re.MULTILINE)
        assert not re.search(r"^\s+value:", entry, re.MULTILINE)
