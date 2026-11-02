"""Cross-runtime contract: admin text and PNG must match n8n's published policy."""

import hashlib
import json
import subprocess
from pathlib import Path

import pytest
from django.core.exceptions import ValidationError

from apps.loyalty.enrollment_invitation import invitation_caption, invitation_image

ROOT = Path(__file__).resolve().parents[1]
URL = "https://luxurysmartapartments.com/my-bookings/rewards/"


@pytest.mark.parametrize("language", ["ar", "en", "fr"])
def test_admin_matches_n8n_caption_and_exact_png(language):
    name = "ضيف تجريبي" if language == "ar" else "Test Guest"
    rendered = json.loads(
        subprocess.check_output(
            [
                "node",
                "-e",
                """
const p=require('./integrations/n8n/loyalty/policy.cjs');
const s=require('./apps/loyalty/invitation_assets/enrollment-owner-review-v3.json').samples;
console.log(JSON.stringify(p.render(s,process.argv[1],process.argv[2])));
""",
                language,
                name,
            ],
            cwd=ROOT,
            text=True,
        )
    )
    assert invitation_caption(language, URL, name) == rendered["caption"]
    assert hashlib.sha256(invitation_image(language)).hexdigest() == rendered["image_sha256"]
    assert name in rendered["caption"]


def test_guest_name_cannot_inject_formatting_and_overlong_caption_is_not_truncated():
    caption = invitation_caption("ar", URL, "<*Guest*>\n\u200fName")
    assert "أهلًا Guest Name" in caption
    with pytest.raises(ValidationError):
        invitation_caption("ar", URL, "x" * 151)
