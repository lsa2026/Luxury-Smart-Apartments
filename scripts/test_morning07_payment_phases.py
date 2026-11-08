"""JSON-lines bridge to a disposable Django test DB. No external calls allowed."""

import json
import os
import sys
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ["DJANGO_SETTINGS_MODULE"] = "config.settings.test"
os.environ["DATABASE_URL"] = "sqlite:///:memory:"

import django  # noqa: E402

django.setup()

from django.test import override_settings  # noqa: E402
from django.test.utils import setup_databases, teardown_databases  # noqa: E402

from tests.morning07_payment_harness import PaymentHarness  # noqa: E402
from tests.test_hyperpay import HYPERPAY_SETTINGS  # noqa: E402


def main():
    with ExitStack() as stack:
        stack.enter_context(
            override_settings(
                **(
                    HYPERPAY_SETTINGS
                    | {
                        "HYPERPAY_ENVIRONMENT": "production",
                        "MORNING07_N8N_ENABLED": True,
                        "MORNING07_PAYMENT_HANDOFF_ENABLED": True,
                        "MORNING07_PAYMENT_WEBHOOK_URL": "https://automation.example.test/payments",
                        "HOSTAWAY_LIVE_BOOKING_ENABLED": True,
                        "HOSTAWAY_DIRECT_CHANNEL_ID": 2000,
                        "SITE_BASE_URL": "https://testserver",
                        "WHATSAPP_CONTACT_NUMBER": "+16502530000",
                    }
                )
            )
        )
        stack.enter_context(
            patch("httpx.Client.request", side_effect=AssertionError("live_network_forbidden"))
        )
        db = setup_databases(verbosity=0, interactive=False)
        try:
            with PaymentHarness(full_journey="--full-journey" in sys.argv) as harness:
                print(json.dumps({"ready": True, "synthetic": True}), flush=True)
                for line in sys.stdin:
                    try:
                        output = harness.phase(json.loads(line))
                    except (ValueError, KeyError, TypeError):
                        output = {"error": "invalid_phase"}
                    print(json.dumps(output), flush=True)
        finally:
            teardown_databases(db, verbosity=0)


if __name__ == "__main__":
    main()
