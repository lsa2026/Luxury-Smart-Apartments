"""Preserve Apple sign-in configuration across production Blueprint syncs."""

import re
from pathlib import Path


def test_render_preserves_apple_sign_in_opt_in_and_secrets():
    blueprint = (Path(__file__).resolve().parents[1] / "render.yaml").read_text()
    for key in (
        "APPLE_SIGN_IN_ENABLED",
        "APPLE_SERVICE_ID",
        "APPLE_KEY_ID",
        "APPLE_TEAM_ID",
        "APPLE_PRIVATE_KEY",
    ):
        entries = re.findall(
            rf"^      - key: {key}\n((?:        [^\n]*\n)+)",
            blueprint,
            re.MULTILINE,
        )
        assert len(entries) == 1, "Only the web service needs Apple credentials"
        assert re.search(r"^\s+sync: false\s*$", entries[0], re.MULTILINE)
        assert not re.search(r"^\s+value:", entries[0], re.MULTILINE)
        assert "fromService:" not in entries[0]
