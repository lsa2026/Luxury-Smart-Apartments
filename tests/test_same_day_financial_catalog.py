"""Render unchanged website copy with inert placeholders, never real capabilities."""

import hashlib
import json
import os
import re
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

from apps.loyalty.enrollment_invitation import invitation_image
from apps.reservations import (
    day_use08_payments,
    day_use_loyalty,
    day_use_payment_messages,
    morning07_payments,
    morning_loyalty,
    morning_payment_messages,
)

pytestmark = pytest.mark.django_db


def test_catalog_from_unchanged_renderers_with_synthetic_fixture(settings, monkeypatch):
    settings.SITE_BASE_URL = "https://example.invalid"
    settings.WHATSAPP_CONTACT_NUMBER = "SYNTHETIC_SUPPORT"
    for module in (morning_payment_messages, day_use_payment_messages):
        monkeypatch.setattr(
            module, "public_url", lambda stay: "https://example.invalid/pay/SYNTHETIC"
        )
    catalog = []
    replacements = {
        "SYNTHETIC_GUEST": "guest_name",
        "SYNTHETIC_APARTMENT": "listing_name",
        "AM-SYNTHETI": "booking_reference",
        "DU-SYNTHETI": "booking_reference",
        "2026-12-03 09:00": "starts_at_local",
        "2026-12-03 12:00": "ends_at_local",
        "2026-12-03": "stay_date",
        "09:00": "check_in_time",
        "12:00": "check_out_time",
        "Asia/Riyadh": "timezone",
        "500.00": "amount_sar",
        "SYNTHETIC_SUPPORT": "support_number",
        "https://example.invalid/pay/SYNTHETIC": "payment_url",
        "https://example.invalid/my-bookings/rewards/": "loyalty_url",
    }
    for scenario, links, paid, loyalty in (
        ("07", morning_payment_messages, morning07_payments, morning_loyalty),
        ("08", day_use_payment_messages, day_use08_payments, day_use_loyalty),
    ):
        for language in ("ar", "en", "fr"):
            stay = SimpleNamespace(
                pk="SYNTHETIC",
                guest_name="SYNTHETIC_GUEST",
                guest_language=language,
                property=SimpleNamespace(
                    **{f"name_{lang}": "SYNTHETIC_APARTMENT" for lang in ("ar", "en", "fr")}
                ),
                starts_at=datetime(2026, 12, 3, 6, tzinfo=UTC),
                ends_at=datetime(2026, 12, 3, 9, tzinfo=UTC),
                time_zone="Asia/Riyadh",
                amount_sar=Decimal("500.00"),
                availability_evidence={"creation_mode": "hostaway_first_v2"},
            )
            for kind, renderer, trigger in (
                (
                    "payment_link",
                    links.render_message,
                    "Explicit current unpaid request, live availability "
                    "and verified core deliveries",
                ),
                (
                    "payment_success",
                    paid.render_success,
                    "Current verified collection and three core deliveries",
                ),
                (
                    "loyalty_invitation",
                    loyalty.caption,
                    "Paid verified stay, core+payment notices delivered, "
                    "shared loyalty eligibility/ledger",
                ),
            ):
                rendered = renderer(stay)
                named = rendered
                for value, field in replacements.items():
                    named = named.replace(value, "{{" + field + "}}")
                variables = []

                def positional(match, variables=variables):
                    field = match.group(1)
                    if field not in variables:
                        variables.append(field)
                    return "{{" + str(variables.index(field) + 1) + "}}"

                body = re.sub(r"\{\{(\w+)\}\}", positional, named)
                restored = body
                # Named source fields reconstruct the exact fixture-rendered body.
                fixture_values = {
                    field: value for value, field in replacements.items() if value in rendered
                }
                for index, field in enumerate(variables, 1):
                    restored = restored.replace("{{" + str(index) + "}}", fixture_values[field])
                assert restored == rendered
                media = None
                if kind == "payment_link" and scenario == "07":
                    image = morning_payment_messages.notice_image(language)
                    media = {
                        "source": f"apps/reservations/morning_assets/payment-{language}.png",
                        "sha256": hashlib.sha256(image).hexdigest(),
                        "mime_type": "image/png",
                    }
                elif kind == "loyalty_invitation":
                    image = invitation_image(language)
                    media = {
                        "source": "apps/loyalty/invitation_assets/enrollment-owner-review-v3.json",
                        "selector": language,
                        "sha256": hashlib.sha256(image).hexdigest(),
                        "mime_type": "image/png",
                    }
                file = Path(renderer.__globals__["__file__"])
                catalog.append(
                    {
                        "scenario": scenario,
                        "language": language,
                        "template_name": kind,
                        "renderer": renderer.__module__ + "." + renderer.__name__,
                        "source_sha256": hashlib.sha256(file.read_bytes()).hexdigest(),
                        "body": body,
                        "variables": [
                            {"index": i, "source": f} for i, f in enumerate(variables, 1)
                        ],
                        "synthetic_rendered_body": rendered,
                        "media": media,
                        "trigger": trigger,
                        "whatsapp_opt_in": {"channel": None, "purpose": None, "timestamp": None},
                        "meta_approved_template": None,
                        "unchanged_source_copy": True,
                        "fixture_roundtrip_verified": True,
                    }
                )
    assert len(catalog) == 18
    folder = Path("artifacts")
    folder.mkdir(exist_ok=True)
    (folder / "site01-same-day-financial-catalog.json").write_text(
        json.dumps(
            {
                "reference": "YC01-SAME-DAY-FINANCIAL-CATALOG-20261122",
                "source_head": os.environ.get("GITHUB_SHA"),
                "synthetic_only": True,
                "published": False,
                "catalog": catalog,
                "limits": [
                    "Template approval and provider formatting are separate; copy was not changed.",
                    "No actual payment URL, guest ID, guest record or access code is included.",
                    "Consent fields describe pre-existing production records; "
                    "they do not describe the optional draft UI.",
                ],
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
