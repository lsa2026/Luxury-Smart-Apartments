"""Generate synthetic invoice previews only (no database/provider access)."""

import argparse
import os
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.test")

import django  # noqa: E402

django.setup()

from apps.notifications.invoice_pdf import render_invoice_pdf  # noqa: E402

parser = argparse.ArgumentParser()
parser.add_argument("output_dir", type=Path)
args = parser.parse_args()
args.output_dir.mkdir(parents=True, exist_ok=True)
for language in ("ar", "en", "fr"):
    invoice = SimpleNamespace(
        language=language,
        issued_at=None,
        number=None,
        snapshot={
            "guest_name": "ضيف تجريبي" if language == "ar" else "Sample Guest",
            "guest_email": "sample@example.invalid",
            "guest_phone": "+1 202 555 0100",
            "platform": "Booking.com",
            "platform_reference": "1234567890",
            "property_names": {
                "ar": "شقة E12 الذكية - غرفتا نوم في حي الملقا",
                "en": "Smart E12 Apartment - Two Bedrooms in Al Malqa",
                "fr": "Appartement E12 - Deux chambres à Al Malqa",
            },
            "check_in": "2026-12-15",
            "check_out": "2026-12-17",
            "nights": 2,
            "total": "1400.00",
            "currency": "SAR",
            "supplier": "مؤسسة الحياة الذكية التجارية",
            "website": "luxurysmartapartments.com",
        },
    )
    (args.output_dir / f"invoice-preview-{language}.pdf").write_bytes(render_invoice_pdf(invoice))
print("Three synthetic previews generated; no bookings or messages created.")
