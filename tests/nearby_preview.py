"""Loopback-only UI QA with fake Places; never uses a Google key or real booking."""

import os
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.test")

import django  # noqa: E402

django.setup()

from django.template.loader import render_to_string  # noqa: E402
from django.utils.translation import override  # noqa: E402

from apps.properties.models import Property  # noqa: E402

ROOT = Path(__file__).parents[1]
ASSETS = {
    "/property-nearby.css": ROOT / "static/css/property-nearby.css",
    "/property-nearby.js": ROOT / "static/js/property-nearby.js",
    "/nearby-preview-mock.js": ROOT / "tests/nearby-preview-mock.js",
}


class PreviewHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path in ASSETS:
            path = ASSETS[parsed.path]
            body = path.read_bytes()
            content_type = "text/css" if path.suffix == ".css" else "text/javascript"
        elif parsed.path == "/":
            language = parse_qs(parsed.query).get("lang", ["ar"])[0]
            if language not in {"ar", "en", "fr"}:
                language = "en"
            p = Property(
                name_ar="شقة نور بريستيج في مراكش",
                name_en="Nour Prestige Marrakech",
                name_fr="Appartement Nour Prestige à Marrakech",
            )
            with override(language):
                dialog = render_to_string(
                    "properties/_nearby_dialog.html",
                    {
                        "property": p,
                        "nearby_config": {
                            "key": "LOCAL-MOCK-NOT-A-KEY",
                            "center": {"lat": 31.647129, "lng": -8.015223},
                            "language": language,
                            "region": "MA",
                            "mapId": "LOCAL-MOCK",
                        },
                    },
                )
            direction = "rtl" if language == "ar" else "ltr"
            body = f"""<!doctype html><html lang="{language}" dir="{direction}"><head>
<meta name="viewport" content="width=device-width,initial-scale=1"><meta charset="utf-8">
<title>Nearby discovery — local mock QA</title><link rel="stylesheet" href="/property-nearby.css">
<style>body{{font-family:Arial;padding:16px}}.button{{display:inline-flex;padding:12px;text-decoration:none}}
.floating{{position:fixed;bottom:20px;right:20px;z-index:999999;background:green;color:white}}</style>
<script src="/nearby-preview-mock.js" defer></script>
<script src="/property-nearby.js" type="module"></script>
</head><body><h1>LOCAL MOCK QA — no live Google results</h1>
<button data-nearby-open>Open nearby map</button><output id="mock-stats">maps=0;queries=0</output>
<button class="floating">WhatsApp / booking QA</button>{dialog}</body></html>""".encode()
            content_type = "text/html; charset=utf-8"
        else:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


if __name__ == "__main__":
    HTTPServer(("127.0.0.1", 8874), PreviewHandler).serve_forever()
