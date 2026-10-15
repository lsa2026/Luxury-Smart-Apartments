"""Offline, synthetic booking-list preview. Never serves live administration."""

import os
import sys
from datetime import timedelta
from decimal import Decimal
from http.server import HTTPServer, SimpleHTTPRequestHandler
from pathlib import Path
from unittest.mock import patch
from urllib.parse import unquote, urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ["DJANGO_SETTINGS_MODULE"] = "config.settings.test"
os.environ["DATABASE_URL"] = "sqlite:///:memory:"
os.environ["CACHE_URL"] = ""
os.environ["REDIS_URL"] = ""

import django  # noqa: E402

django.setup()

from django.contrib.auth import get_user_model  # noqa: E402
from django.contrib.staticfiles import finders  # noqa: E402
from django.core.management import call_command  # noqa: E402
from django.test import Client, override_settings  # noqa: E402
from django.urls import reverse  # noqa: E402
from django.utils import timezone  # noqa: E402

from apps.reservations.services.hostaway_booking import prepare_local_reservation  # noqa: E402
from tests.test_booking_models_services import make_property  # noqa: E402
from tests.test_hostaway_booking_phase5 import make_intent  # noqa: E402
from tests.test_operations_booking_summary import payment, refund  # noqa: E402

OUTPUT = ROOT / "outputs/booking-summary-20261016"


def render_preview():
    call_command("migrate", verbosity=0)
    owner = get_user_model().objects.create_superuser(username="معاينة", password=None)
    property_obj = make_property()
    property_obj.name_ar = "شقة الملقا العائلية — ثلاث غرف نوم"
    property_obj.save()
    for index, total in enumerate(("2100", "700", "3001", "1400")):
        booking = prepare_local_reservation(make_intent(property_obj=property_obj))
        booking.booking_intent.guest_first_name = "ضيف تجريبي"
        booking.booking_intent.guest_last_name = str(index + 1)
        booking.booking_intent.save()
        booking.total_price = Decimal(total)
        booking.nights = 1 if index == 1 else 3
        booking.check_in = timezone.localdate() + timedelta(days=2)
        booking.check_out = booking.check_in + timedelta(days=booking.nights)
        booking.normalized_status = "confirmed"
        booking.hostaway_reservation_id = 99001 + index
        booking.payment_status = "paid" if index < 3 else ""
        if index == 2:
            booking.currency = "MAD"
        booking.save()
        if index < 3:
            payment(booking, amount=Decimal("1000") if index == 2 else booking.total_price)
        if index == 0:
            refund(booking)
    client = Client()
    client.force_login(owner)
    with override_settings(HOSTAWAY_WEBSITE_RESERVATION_SYNC_ENABLED=False):
        with patch(
            "httpx.Client.send", side_effect=AssertionError("Offline preview: no provider access")
        ):
            response = client.get(reverse("notifications:booking_list"))
    assert response.status_code == 200
    OUTPUT.mkdir(parents=True, exist_ok=True)
    (OUTPUT / "bookings.html").write_bytes(response.content)


class PreviewHandler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(ROOT), **kwargs)

    def guess_type(self, path):
        kind = super().guess_type(path)
        return kind + "; charset=utf-8" if kind.startswith("text/") else kind

    def translate_path(self, path):
        name = unquote(urlsplit(path).path).removeprefix("/static/")
        if path.startswith("/static/") and ".." not in name and (asset := finders.find(name)):
            return asset
        return super().translate_path(path)

    def do_GET(self):
        path = unquote(urlsplit(self.path).path)
        if (
            "\\" in path
            or ".." in path.split("/")
            or not path.startswith(("/static/", "/outputs/booking-summary-20261016/"))
        ):
            self.send_error(404)
            return
        super().do_GET()

    def do_POST(self):
        self.send_error(405, "Offline preview: actions disabled")


if __name__ == "__main__":
    render_preview()
    print("Synthetic preview ready on http://127.0.0.1:8769", flush=True)
    HTTPServer(("127.0.0.1", 8769), PreviewHandler).serve_forever()
