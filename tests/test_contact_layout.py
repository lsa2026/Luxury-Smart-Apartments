"""Render synthetic contact pages for isolated responsive browser checks."""

import os
import re
from pathlib import Path

import pytest
from django.contrib.staticfiles import finders
from django.urls import reverse

from tests import test_contact_workspace as workspace
from tests.test_phase9_notifications import make_contact

client = workspace.client
owner = workspace.owner
pytestmark = pytest.mark.django_db


def _standalone(html):
    def stylesheet(match):
        url = match.group(1).split("?")[0]
        if not url.startswith("/static/"):
            return ""
        filename = finders.find(url.removeprefix("/static/"))
        if not filename or not filename.endswith(".css"):
            return ""
        source = Path(filename)

        def asset(match):
            value = match.group(1).strip(" \"'")
            if value.startswith(("data:", "http:", "https:", "#")):
                return match.group(0)
            return f"url({(source.parent / value).resolve().as_uri()})"

        return "<style>" + re.sub(r"url\(([^)]+)\)", asset, source.read_text()) + "</style>"

    def image(match):
        filename = finders.find(match.group(1).split("?")[0])
        return f'src="{Path(filename).as_uri()}"' if filename else ""

    html = re.sub(r'<link[^>]*href="([^"]+)"[^>]*>', stylesheet, html)
    html = re.sub(r"<script\b[^>]*>[\s\S]*?</script>", "", html)
    return re.sub(r'src="/static/([^"]+)"', image, html)


def test_render_contact_layout(client, tmp_path, settings):
    settings.OPERATIONS_EMAIL = "business@example.invalid"
    rows = [
        make_contact(
            name="ضيف تجريبي",
            subject="استفسار عن الإقامة والخدمات المتاحة",
            message=(
                "السلام عليكم، نرغب في الاستفسار عن إقامة عائلية في الرياض.\n\n"
                "هل تتوفر شقة لخمسة ضيوف؟ وما الخدمات المتاحة أثناء الإقامة؟\n"
                "يسعدنا تلقي تفاصيل الأسعار وشروط الوصول.\n\n"
                "Hello, we would like to ask about a family stay in Riyadh.\n"
                "Please share the available services and check-in information.\n\nشكرًا لفريقكم."
            ),
        )
        for _ in range(3)
    ]
    output = Path(os.environ.get("LSA_CONTACT_VISUAL_DIR", tmp_path))
    output.mkdir(parents=True, exist_ok=True)
    for name, url in (
        ("detail", reverse("admin:core_contactmessage_change", args=[rows[1].pk])),
        ("list", reverse("admin:core_contactmessage_changelist")),
    ):
        response = client.get(url)
        assert response.status_code == 200
        html = _standalone(response.content.decode())
        assert "<script" not in html
        (output / f"{name}.html").write_text(html)
