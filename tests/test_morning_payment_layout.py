"""Render one synthetic morning detail for phone/tablet layout validation."""

import os
from pathlib import Path
from urllib.parse import urlsplit

import pytest
from django.test import Client
from django.urls import reverse

from apps.payments.morning_payments import public_url
from tests.test_contact_layout import _standalone
from tests.test_morning_payment_messages import isolated  # noqa: F401
from tests.test_morning_stays import create

pytestmark = pytest.mark.django_db


def test_render_morning_payment_layout(tmp_path, settings):
    settings.GOOGLE_PLACES_ENABLED = True
    settings.GOOGLE_PLACES_API_KEY = "synthetic-no-network"
    settings.GOOGLE_PLACES_REQUIRE_SHARED_CACHE = False
    stay = create()
    client = Client()
    client.force_login(stay.created_by)
    response = client.get(reverse("notifications:morning_detail", args=[stay.pk]))
    assert response.status_code == 200
    html = _standalone(response.content.decode())
    assert "send_payment_link" in html and "معاينة الرسالة التي ستصل إلى الضيف" in html
    output = Path(os.environ.get("LSA_CONTACT_VISUAL_DIR", tmp_path))
    output.mkdir(parents=True, exist_ok=True)
    script = Path("static/js/checkout-address.js").resolve().as_uri()
    module = f'<script type="module" src="{script}"></script>'
    (output / "morning.html").write_text(html + module)
    create_response = client.get(reverse("notifications:morning_create"))
    guest_response = Client().get(urlsplit(public_url(stay)).path)
    for name, page in [("morning-create", create_response), ("morning-guest", guest_response)]:
        assert page.status_code == 200
        assert b"data-checkout-address hidden" in page.content
        (output / f"{name}.html").write_text(_standalone(page.content.decode()) + module)
