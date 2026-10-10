"""Private, embedded-font PDF renderer. No network, tracking, or payment mutation."""

from decimal import Decimal
from functools import lru_cache
from io import BytesIO
from pathlib import Path

from django.utils import timezone

COPY = {
    "ar": {
        "title": "فاتورة إقامة",
        "preview": "معاينة — لم تُصدر بعد",
        "guest": "بيانات الضيف",
        "booking": "بيانات الحجز",
        "reference": "مرجع حجز المنصة",
        "stay": "تفاصيل الإقامة",
        "arrival": "الدخول",
        "departure": "الخروج",
        "nights": "الليالي",
        "description": "الإقامة في الشقة",
        "total": "الإجمالي",
        "thanks": "شكرًا لاختياركم Luxury Living",
        "date": "تاريخ الإصدار",
        "notice": "لا يغني هذا المستند عن الفاتورة الضريبية النظامية عند وجوبها.",
    },
    "en": {
        "title": "Accommodation invoice",
        "preview": "PREVIEW — not issued",
        "guest": "Guest details",
        "booking": "Booking details",
        "reference": "Platform booking reference",
        "stay": "Stay details",
        "arrival": "Check-in",
        "departure": "Check-out",
        "nights": "Nights",
        "description": "Apartment accommodation",
        "total": "Total",
        "thanks": "Thank you for choosing Luxury Living",
        "date": "Issued on",
        "notice": "This document does not replace a statutory tax invoice where required.",
    },
    "fr": {
        "title": "Facture de séjour",
        "preview": "APERÇU — non émis",
        "guest": "Informations du client",
        "booking": "Informations de réservation",
        "reference": "Référence de réservation de la plateforme",
        "stay": "Détails du séjour",
        "arrival": "Arrivée",
        "departure": "Départ",
        "nights": "Nuits",
        "description": "Hébergement en appartement",
        "total": "Total",
        "thanks": "Merci d’avoir choisi Luxury Living",
        "date": "Date d’émission",
        "notice": "Ce document ne remplace pas la facture fiscale légale lorsqu’elle est requise.",
    },
}


@lru_cache(maxsize=1)
def _fonts():
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont

    assets = Path(__file__).with_name("invoice_assets")
    pdfmetrics.registerFont(TTFont("LL-Regular", str(assets / "Amiri-Regular.ttf")))
    pdfmetrics.registerFont(TTFont("LL-Bold", str(assets / "Amiri-Bold.ttf")))
    return assets


def _visual(text):
    import arabic_reshaper
    from bidi.algorithm import get_display

    text = str(text)
    if any("\u0600" <= character <= "\u06ff" for character in text):
        return get_display(arabic_reshaper.reshape(text))
    return text


def render_invoice_pdf(invoice):
    from reportlab.lib.colors import HexColor
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfbase.pdfmetrics import stringWidth
    from reportlab.pdfgen.canvas import Canvas

    assets = _fonts()
    out = BytesIO()
    canvas = Canvas(out, pagesize=A4, pageCompression=1)
    canvas.setTitle("Luxury Living — " + COPY[invoice.language]["title"])
    canvas.setAuthor("Luxury Living")
    width, height = A4
    data = invoice.snapshot
    words = COPY[invoice.language]
    rtl = invoice.language == "ar"
    emerald, gold, muted = map(HexColor, ("#123c35", "#bc974c", "#716c61"))
    canvas.setFillColor(HexColor("#fffdf8"))
    canvas.rect(0, 0, width, height, fill=1, stroke=0)

    def text(value, x, y, *, size=12, bold=False, color=emerald, max_width=490, align=None):
        font = "LL-Bold" if bold else "LL-Regular"
        value = _visual(value)
        # Exact width fitting protects long booking references/contact fields from clipping.
        measured = stringWidth(value, font, size)
        if measured > max_width:
            size = max(5.5, size * max_width / measured)
        canvas.setFillColor(color)
        canvas.setFont(font, size)
        if (align or ("right" if rtl else "left")) == "right":
            canvas.drawRightString(x, y, value)
        else:
            canvas.drawString(x, y, value)

    def paragraph(value, x, y, *, max_width, size=12, bold=False, max_lines=2):
        while True:
            lines, line = [], ""
            for word in str(value).split():
                candidate = f"{line} {word}".strip()
                if (
                    line
                    and stringWidth(_visual(candidate), "LL-Bold" if bold else "LL-Regular", size)
                    > max_width
                ):
                    lines.append(line)
                    line = word
                else:
                    line = candidate
            lines.append(line)
            if len(lines) <= max_lines or size <= 6:
                break
            size -= 0.5
        for line in lines:
            text(line, x, y, size=size, bold=bold, max_width=max_width)
            y -= size * 1.5
        return y

    # The supplied approved Figma logo is used verbatim, not recreated.
    canvas.drawImage(
        str(assets / "logo.png"),
        width - 189,
        height - 173,
        150,
        150,
        preserveAspectRatio=True,
        mask="auto",
    )
    text("LUXURY LIVING", 42, height - 65, size=18, bold=True, align="left")
    text("SMART APARTMENTS", 42, height - 88, size=10, color=gold, align="left")
    text(words["title"], 42, height - 122, size=24, bold=True, align="left", max_width=340)
    canvas.setStrokeColor(gold)
    canvas.setLineWidth(1)
    canvas.line(42, height - 163, width - 42, height - 163)
    edge = width - 60 if rtl else 60
    text(data["supplier"], edge, height - 190, size=13, bold=True)
    text(data["website"], edge, height - 209, size=10, color=muted)
    if invoice.issued_at:
        text(invoice.number, edge, height - 235, size=10)
        stamp = timezone.localtime(invoice.issued_at).strftime("%Y-%m-%d  %H:%M %Z")
        text(f"{words['date']}: {stamp}", edge, height - 254, size=10, color=muted)
    else:
        text(words["preview"], edge, height - 242, size=12, color=gold, bold=True)

    y = height - 291
    card_width = (width - 96 - 16) / 2
    for x in (48, 48 + card_width + 16):
        canvas.setFillColor(HexColor("#f3f0e7"))
        canvas.roundRect(x, y - 110, card_width, 110, 12, fill=1, stroke=0)
    guest_x = width - 58 if rtl else 58
    booking_x = 48 + card_width - 10 if rtl else 48 + card_width + 26
    text(words["guest"], guest_x, y - 22, bold=True, size=13, max_width=card_width - 20)
    paragraph(data["guest_name"], guest_x, y - 44, bold=True, max_width=card_width - 20, size=11)
    text(data["guest_email"], guest_x, y - 82, size=9, max_width=card_width - 20, color=muted)
    text(data["guest_phone"], guest_x, y - 99, size=10, max_width=card_width - 20, color=muted)
    text(words["booking"], booking_x, y - 22, bold=True, size=13, max_width=card_width - 20)
    text(data["platform"], booking_x, y - 44, size=12, max_width=card_width - 20)
    text(words["reference"], booking_x, y - 66, size=9, color=muted, max_width=card_width - 20)
    text(
        data["platform_reference"] or "—",
        booking_x,
        y - 93,
        size=15,
        bold=True,
        max_width=card_width - 20,
    )

    y -= 146
    text(words["stay"], edge, y, bold=True, size=15)
    paragraph(
        data["property_names"][invoice.language],
        edge,
        y - 26,
        max_width=width - 96,
        bold=True,
        size=16,
    )
    y = 327
    positions = [width - 48, width / 2 + 42, 170] if rtl else [48, 230, 412]
    for x, label, value in zip(
        positions,
        (words["arrival"], words["departure"], words["nights"]),
        (data["check_in"], data["check_out"], data["nights"]),
        strict=True,
    ):
        text(label, x, y, size=10, color=muted, max_width=135)
        text(value, x, y - 23, bold=True, size=14, max_width=135)
    y -= 63
    canvas.setFillColor(emerald)
    canvas.roundRect(48, y - 35, width - 96, 35, 6, stroke=0, fill=1)
    text(words["description"], edge, y - 24, color=HexColor("#ffffff"), bold=True, size=12)
    y -= 62
    text(data["property_names"][invoice.language], edge, y, size=12)
    text(f"{data['check_in']} - {data['check_out']}", edge, y - 23, size=11, color=muted)
    y = 149
    canvas.setFillColor(HexColor("#f0e6cb"))
    canvas.roundRect(48, y - 67, width - 96, 67, 10, stroke=0, fill=1)
    text(words["total"], edge, y - 22, size=12, bold=True)
    text(f"{Decimal(data['total']):,.2f} {data['currency']}", edge, y - 51, bold=True, size=25)
    text(words["thanks"], edge, 54, size=12, bold=True)
    text(words["notice"], edge, 32, size=9, color=muted)
    canvas.showPage()
    canvas.save()
    return out.getvalue()
