"""Payment recovery must retain its approved gateway without sharing it with beat."""

import re
from pathlib import Path

import pytest


def service(name):
    blueprint = (Path(__file__).resolve().parents[1] / "render.yaml").read_text()
    return blueprint.split(f"    name: {name}\n", 1)[1].split("  - type:", 1)[0]


@pytest.mark.parametrize(
    "key",
    [
        "HYPERPAY_ENABLED",
        "HYPERPAY_ENTITY_ID",
        "HYPERPAY_ACCESS_TOKEN",
        "HYPERPAY_APPLE_PAY_PRODUCTION_ENABLED",
    ],
)
def test_worker_payment_verification_reuses_web_gateway(key):
    worker = service("lsa-worker")
    entry = re.search(rf"^      - key: {key}\n((?:        [^\n]*\n)+)", worker, re.MULTILINE)
    assert entry
    assert "fromService:" in entry[1]
    assert "name: luxury-smart-apartments" in entry[1]
    assert f"envVarKey: {key}" in entry[1]
    assert "value:" not in entry[1]


@pytest.mark.parametrize("name", ["luxury-smart-apartments", "lsa-worker"])
@pytest.mark.parametrize(
    "key",
    [
        "RESERVATION_PAYMENT_LINKS_ENABLED",
        "MANUAL_PAYMENT_RECEIPTS_ENABLED",
        "RESERVATION_PAYMENT_LINK_TTL_SECONDS",
    ],
)
def test_rollout_flags_remain_owner_managed(name, key):
    assert f"- key: {key}\n        sync: false" in service(name)


def test_payment_recovery_keeps_scheduler_and_domain_file_isolated():
    assert "HYPERPAY" not in service("lsa-beat")
    assert "APPLE_PAY_DOMAIN_ASSOCIATION_FILE" not in service("lsa-worker")
