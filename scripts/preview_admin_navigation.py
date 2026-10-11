"""Render synthetic admin pages offline; serve only previews/static on loopback."""

import os
import sys
from datetime import timedelta
from http.server import HTTPServer, SimpleHTTPRequestHandler
from pathlib import Path
from unittest.mock import patch
from urllib.parse import unquote, urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ["DJANGO_SETTINGS_MODULE"] = "config.settings.test"
os.environ["DATABASE_URL"] = "sqlite:///:memory:"

import django  # noqa: E402

django.setup()

from django.contrib.auth import get_user_model  # noqa: E402
from django.contrib.staticfiles import finders  # noqa: E402
from django.core.management import call_command  # noqa: E402
from django.test import Client, override_settings  # noqa: E402
from django.urls import reverse  # noqa: E402
from django.utils import timezone  # noqa: E402

from apps.notifications.models import GuestInvoice  # noqa: E402
from tests.test_booking_modifications_phase6 import confirmed_reservation  # noqa: E402


def render_previews():
    call_command("migrate", verbosity=0)
    user = get_user_model().objects.create_superuser(
        username="preview-owner", email="preview@example.invalid", password=None
    )
    reservation = confirmed_reservation()
    intent = reservation.booking_intent
    intent.guest_first_name, intent.guest_last_name = "ضيف", "تجريبي"
    intent.guest_email, intent.guest_phone = "preview@example.invalid", "+966501234567"
    intent.save()
    reservation.check_in = timezone.now().date() - timedelta(days=4)
    reservation.check_out = timezone.now().date() - timedelta(days=2)
    reservation.nights = 2
    reservation.save()
    GuestInvoice.objects.create(
        hostaway_reservation_id=reservation.hostaway_reservation_id,
        created_by=user,
        issued_at=timezone.now(),
        number="PREVIEW-ONLY",
        snapshot={"guest_email": "preview@example.invalid", "guest_phone": "+966501234567"},
    )
    client = Client()
    client.force_login(user)
    output = ROOT / "outputs/admin-navigation-20261011"
    output.mkdir(parents=True, exist_ok=True)
    with override_settings(HOSTAWAY_WEBSITE_RESERVATION_SYNC_ENABLED=False):
        with patch(
            "apps.integrations.hostaway.client.HostawayClient.__init__",
            side_effect=AssertionError("Offline previews may not call Hostaway"),
        ):
            for name, url in {
                "home": reverse("notifications:hub"),
                "bookings": reverse("notifications:booking_list"),
                "booking": reverse("notifications:booking_detail", args=[reservation.pk]),
            }.items():
                response = client.get(url)
                assert response.status_code == 200
                (output / f"{name}.html").write_bytes(response.content)
    return output


class PreviewHandler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(ROOT), **kwargs)

    def guess_type(self, path):
        kind = super().guess_type(path)
        return kind + "; charset=utf-8" if kind.startswith("text/") else kind

    def translate_path(self, path):
        if path.startswith("/static/"):
            name = path.split("?", 1)[0][len("/static/") :]
            if ".." not in name and (asset := finders.find(name)):
                return asset
        return super().translate_path(path)

    def do_GET(self):
        path = unquote(urlsplit(self.path).path)
        if (
            "\\" in path
            or ".." in path.split("/")
            or not path.startswith(("/static/", "/outputs/admin-navigation-20261011/"))
        ):
            self.send_error(404)
            return
        super().do_GET()

    def do_POST(self):
        self.send_error(405, "Offline preview: actions are disabled")


if __name__ == "__main__":
    render_previews()
    print("Offline synthetic previews ready on http://127.0.0.1:8767", flush=True)
    HTTPServer(("127.0.0.1", 8767), PreviewHandler).serve_forever()
