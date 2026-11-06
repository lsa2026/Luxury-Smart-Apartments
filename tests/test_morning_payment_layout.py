"""Render one synthetic morning detail for phone/tablet layout validation."""

import os
from pathlib import Path

import pytest
from django.test import Client
from django.urls import reverse

from tests.test_contact_layout import _standalone
from tests.test_morning_payment_messages import isolated  # noqa: F401
from tests.test_morning_stays import create

pytestmark = pytest.mark.django_db


def test_render_morning_payment_layout(tmp_path):
    stay = create()
    client = Client()
    client.force_login(stay.created_by)
    response = client.get(reverse("notifications:morning_detail", args=[stay.pk]))
    assert response.status_code == 200
    html = _standalone(response.content.decode())
    assert "send_payment_link" in html and "معاينة الرسالة التي ستصل إلى الضيف" in html
    output = Path(os.environ.get("LSA_CONTACT_VISUAL_DIR", tmp_path))
    output.mkdir(parents=True, exist_ok=True)
    (output / "morning.html").write_text(html)
