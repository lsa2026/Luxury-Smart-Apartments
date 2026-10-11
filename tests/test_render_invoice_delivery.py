"""Invoice sends require the same existing integration in the task executor."""

import re
from pathlib import Path

import pytest


@pytest.mark.parametrize(
    "key",
    [
        "ULTRAMSG_ENABLED",
        "ULTRAMSG_API_BASE_URL",
        "ULTRAMSG_INSTANCE_ID",
        "ULTRAMSG_TOKEN",
        "ACCOUNTING_WHATSAPP_NUMBER",
    ],
)
def test_invoice_worker_reuses_web_whatsapp_settings(key):
    blueprint = (Path(__file__).resolve().parents[1] / "render.yaml").read_text()
    worker = blueprint.split("    name: lsa-worker\n", 1)[1].split("  - type:", 1)[0]
    entry = re.search(rf"^      - key: {key}\n((?:        [^\n]*\n)+)", worker, re.MULTILINE)
    assert entry
    assert "fromService:" in entry[1]
    assert "name: luxury-smart-apartments" in entry[1]
    assert f"envVarKey: {key}" in entry[1]
