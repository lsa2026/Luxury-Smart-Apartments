"""Deterministic booking-bound access PDF; memory only, no network or logs.

The input is the exact14-field source contract, not a model/caller body.
Source eligibility, recipient/revision binding and private delivery are separate.
"""

import hashlib
import json
import threading
from io import BytesIO
from pathlib import Path
from urllib.parse import urlsplit

VERSION = "yc19-access-guide-pdf-2"
FIELDS = (
    "guest_name",
    "booking_reference",
    "listing_name",
    "arrival_date",
    "departure_date",
    "check_in_time",
    "check_out_time",
    "timezone",
    "property_address",
    "map_url",
    "video_url",
    "gate_code",
    "wifi_net",
    "wifi_pass",
)
WORDS = {
    "ar": {
        "title": "دليل الوصول ومرافق الإقامة",
        "stay": "الحجز والإقامة",
        "guest_name": "الضيف",
        "booking_reference": "مرجع الحجز",
        "listing_name": "العقار",
        "arrival_date": "تاريخ الدخول",
        "departure_date": "تاريخ الخروج",
        "check_in_time": "وقت الدخول",
        "check_out_time": "وقت الخروج",
        "timezone": "المنطقة الزمنية",
        "property_address": "عنوان العقار",
        "map_url": "موقع العقار على الخريطة",
        "video_url": "فيديو الوصول",
        "gate_code": "رمز بوابة المبنى",
        "wifi_net": "شبكة واي فاي",
        "wifi_pass": "كلمة مرور الشبكة",
        "note": "يقدم فريق العقار بيانات باب الشقة بعد استكمال إجراءات الدخول المطلوبة.",
        "support": "للمساعدة",
        "private": "مستند خاص بالحجز؛ احتفظ ببيانات الدخول لنفسك.",
    },
    "en": {
        "title": "Arrival and accommodation guide",
        "stay": "Booking and stay",
        "guest_name": "Guest",
        "booking_reference": "Booking reference",
        "listing_name": "Property",
        "arrival_date": "Arrival date",
        "departure_date": "Departure date",
        "check_in_time": "Check-in time",
        "check_out_time": "Check-out time",
        "timezone": "Time zone",
        "property_address": "Property address",
        "map_url": "Property map",
        "video_url": "Arrival video",
        "gate_code": "Building gate code",
        "wifi_net": "Wi-Fi network",
        "wifi_pass": "Wi-Fi password",
        "note": (
            "Our property team provides the apartment door details after completing "
            "the check-in requirements."
        ),
        "support": "Support",
        "private": "Private booking document. Keep access details to yourself.",
    },
    "fr": {
        "title": "Guide d’arrivée et du logement",
        "stay": "Réservation et séjour",
        "guest_name": "Client",
        "booking_reference": "Référence de réservation",
        "listing_name": "Logement",
        "arrival_date": "Date d’arrivée",
        "departure_date": "Date de départ",
        "check_in_time": "Heure d’arrivée",
        "check_out_time": "Heure de départ",
        "timezone": "Fuseau horaire",
        "property_address": "Adresse du logement",
        "map_url": "Carte du logement",
        "video_url": "Vidéo d’accès",
        "gate_code": "Code du portail de l’immeuble",
        "wifi_net": "Réseau Wi-Fi",
        "wifi_pass": "Mot de passe Wi-Fi",
        "note": (
            "L’équipe fournit les informations de la porte après les formalités d’arrivée requises."
        ),
        "support": "Assistance",
        "private": "Document privé de réservation. Gardez les accès pour vous.",
    },
}
_LOCK = threading.RLock()
_LOADED = False


def _fonts():
    global _LOADED
    if _LOADED:
        return
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont

    root = Path(__file__).with_name("access-guide-fonts")
    manifest = json.loads((root / "MANIFEST.json").read_text(encoding="utf-8"))
    for name, font in [
        ("Amiri-Regular.ttf", "LSA-Access2"),
        ("Amiri-Bold.ttf", "LSA-Access2-Bold"),
    ]:
        raw = (root / name).read_bytes()
        if hashlib.sha256(raw).hexdigest() != manifest[name]:
            raise ValueError("access_font_not_verified")
        pdfmetrics.registerFont(TTFont(font, BytesIO(raw)))
    _LOADED = True


def validate_fields(values, language):
    if language not in WORDS or not isinstance(values, dict):
        raise ValueError("access_source_required")
    for key in FIELDS:
        value = values.get(key)
        if (
            not isinstance(value, str)
            or not value.strip()
            or len(value) > 2048
            or any(ord(c) < 32 and c not in "\n\t\r" for c in value)
        ):
            raise ValueError("access_source_field_missing")
    for key in ("map_url", "video_url"):
        u = urlsplit(values[key])
        if (
            u.scheme != "https"
            or not u.hostname
            or u.username
            or u.password
            or any(c.isspace() for c in values[key])
        ):
            raise ValueError("access_source_url_invalid")
    from zoneinfo import ZoneInfo

    try:
        ZoneInfo(values["timezone"])
    except Exception:
        raise ValueError("access_source_timezone_invalid") from None


def _visual(value):
    if any("\u0600" <= c <= "\u06ff" for c in value):
        import arabic_reshaper
        from bidi.algorithm import get_display

        return get_display(arabic_reshaper.reshape(value))
    return value


def render_access_guide(values, language):
    validate_fields(values, language)
    with _LOCK:
        _fonts()
        from reportlab.lib.colors import HexColor
        from reportlab.lib.pagesizes import A4
        from reportlab.pdfbase.pdfmetrics import stringWidth
        from reportlab.pdfgen.canvas import Canvas

        out = BytesIO()
        c = Canvas(out, pagesize=A4, pageCompression=0, invariant=1)
        c.setTitle("Luxury Smart Apartments - Arrival guide - " + language)
        c.setAuthor("Luxury Smart Apartments")
        w, h = A4
        words = WORDS[language]
        rtl = language == "ar"
        page = 0
        y = 0
        green = HexColor("#183D32")
        gold = HexColor("#AD8947")

        def draw(value, y, size=12, bold=False, link=None):
            text = _visual(value)
            font = "LSA-Access2-Bold" if bold else "LSA-Access2"
            c.setFont(font, size)
            c.setFillColor(green)
            is_rtl = rtl and any("\u0600" <= ch <= "\u06ff" for ch in value)
            if is_rtl:
                c.drawRightString(w - 48, y, text)
            else:
                c.drawString(48, y, text)
            if link:
                width = min(w - 96, stringWidth(text, font, size))
                c.linkURL(link, (48, y - 3, 48 + width, y + size + 2), relative=0)

        def footer():
            c.setStrokeColor(gold)
            c.line(48, 46, w - 48, 46)
            draw(words["private"], 31, 9)
            c.setFont("LSA-Access2", 9)
            c.drawRightString(w - 48, 17, str(page))

        def new_page():
            nonlocal y, page
            if page:
                footer()
                c.showPage()
            page += 1
            if page > 8:
                raise ValueError("access_document_layout_limit")
            c.setFillColor(HexColor("#F4F6F2"))
            c.rect(0, 0, w, h, fill=1, stroke=0)
            c.setFillColor(green)
            c.rect(0, h - 91, w, 91, fill=1, stroke=0)
            c.setFillColor(HexColor("#FFFFFF"))
            c.setFont("LSA-Access2-Bold", 21)
            c.drawString(48, h - 37, "Luxury Smart Apartments")
            c.setFont("LSA-Access2-Bold", 17)
            if rtl:
                c.drawRightString(w - 48, h - 70, _visual(words["title"]))
            else:
                c.drawString(48, h - 70, words["title"])
            y = h - 119

        def wrap(value, size=12):
            # Wrap every character without dropping or shortening source values.
            lines = []
            for paragraph in value.replace("\r", "").split("\n"):
                line = ""
                for char in paragraph:
                    candidate = line + char
                    if line and stringWidth(_visual(candidate), "LSA-Access2", size) > w - 96:
                        lines.append(line)
                        line = char
                    else:
                        line = candidate
                lines.append(line)
            return lines

        new_page()
        for key in FIELDS:
            lines = wrap(values[key], 11 if key in ("map_url", "video_url") else 12)
            if y < 105:
                new_page()
            draw(words[key], y, 12, True)
            y -= 16
            for line in lines:
                if y < 68:
                    new_page()
                draw(
                    line,
                    y,
                    11 if key in ("map_url", "video_url") else 12,
                    link=values[key] if key in ("map_url", "video_url") else None,
                )
                y -= 16
            y -= 6
        for value in (words["note"], words["support"], "+966501205651"):
            for line in wrap(value, 11):
                if y < 68:
                    new_page()
                draw(line, y, 11)
                y -= 18
            y -= 6
        footer()
        c.save()
        raw = out.getvalue()
        if not raw.startswith(b"%PDF-") or not 1 <= len(raw) <= 2_000_000:
            raise ValueError("access_document_size")
        return raw
