"""Keep Apple Pay authorization behind the production rollout gate."""

from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest
from django.test import override_settings

from apps.payments.hyperpay.service import HyperPayService


@pytest.mark.parametrize("enabled", [False, True])
@pytest.mark.parametrize("brand", ["MADA", "VISA", "MASTER", "APPLEPAY", "AMEX"])
def test_production_verification_respects_apple_pay_gate(enabled, brand):
    attempt = SimpleNamespace(
        amount=Decimal("100.00"), currency="SAR", merchant_transaction_id="approved-transaction"
    )
    document = {
        "amount": "100.00",
        "currency": "SAR",
        "paymentType": "DB",
        "merchantTransactionId": attempt.merchant_transaction_id,
        "entityId": "approved-entity",
        "paymentBrand": brand,
    }
    with override_settings(
        HYPERPAY_ENVIRONMENT="production",
        HYPERPAY_APPLE_PAY_PRODUCTION_ENABLED=enabled,
        HYPERPAY_ALLOWED_BRANDS=("MADA", "VISA", "MASTER", "APPLEPAY"),
        HYPERPAY_PAYMENT_TYPE="DB",
        HYPERPAY_ENTITY_ID="approved-entity",
    ):
        mismatch = HyperPayService._verification_mismatch(attempt, document)
    rejected = brand == "AMEX" or (brand == "APPLEPAY" and not enabled)
    assert mismatch == ("payment_brand_mismatch" if rejected else "")


def test_blueprint_preserves_web_only_apple_pay_opt_ins():
    blueprint = (Path(__file__).resolve().parents[1] / "render.yaml").read_text()
    for key in ("HYPERPAY_APPLE_PAY_PRODUCTION_ENABLED", "APPLE_PAY_DOMAIN_ASSOCIATION_FILE"):
        assert blueprint.count(f"- key: {key}\n        sync: false") == 1
