"""Render the actual checkout module for visual QA, without DB or gateway calls."""

import argparse
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ["DJANGO_SETTINGS_MODULE"] = "config.settings.test"

import django  # noqa: E402

django.setup()

from django.template import Context, Template  # noqa: E402
from django.utils.translation import override  # noqa: E402

parser = argparse.ArgumentParser()
parser.add_argument("output_dir", type=Path)
args = parser.parse_args()
args.output_dir.mkdir(parents=True, exist_ok=True)
root = Path(__file__).resolve().parents[1]
asset_url = os.path.relpath(root / "static", args.output_dir).replace("\\", "/")
source = (root / "templates/payments/hyperpay_checkout.html").read_text(encoding="utf-8")
module = source[
    source.index('<article class="checkout__payment"') : source.index("</article>") + 10
]
tabs_source = (root / "static/js/site.js").read_text(encoding="utf-8")
tabs_source = tabs_source[
    tabs_source.index("const checkoutMethods =") : tabs_source.index("// Floating WhatsApp")
]
for language in ("ar", "en", "fr"):
    with override(language):
        html = Template("{% load i18n static %}" + module).render(
            Context({"apple_pay_enabled": True, "result_url": "#"})
        )
    # Stub only the hosted widget for this local visual artifact. No payment session.
    # Static label is QA-only; production retains the gateway's SDK-owned button.
    html = re.sub(
        r'<form[^>]+data-brands="(MADA|VISA MASTER|APPLEPAY)"></form>',
        lambda match, language=language: (
            '<div class="wpwl-form"><button class="qa-apple-preview" disabled>'
            '<img src="../payment-methods-preview-20261012/apple-pay-button-label.svg"'
            ' alt="Apple Pay — preview only"></button></div>'
            if match[1] == "APPLEPAY"
            else '<div class="wpwl-form"><p>Hosted card fields — visual preview only</p>'
            '<button class="wpwl-button-pay" disabled>Preview only</button></div>'
        ),
        html,
    )
    html = html.replace("/static/", asset_url + "/")
    direction = "rtl" if language == "ar" else "ltr"
    result = f"""<!doctype html><html lang="{language}" dir="{direction}"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Checkout visual QA — no payment</title>
<link rel="stylesheet" href="{asset_url}/css/site.css">
<link rel="stylesheet" href="{asset_url}/css/checkout-methods.css">
<style>body{{padding:32px 16px;background:#f7f5f0}}
.checkout{{max-width:900px;margin:auto}}
.qa-note{{text-align:center;font-size:12px;color:#69707c;margin-bottom:22px}}</style>
<style>.qa-apple-preview{{width:100%;height:54px;background:#000;border:0;border-radius:12px;opacity:1}}
.qa-apple-preview img{{width:238px;max-width:94%;height:52px;object-fit:contain}}
@media(max-width:640px){{.qa-apple-preview{{height:49px;border-radius:10px}}}}</style>
</head><body><p class="qa-note">معاينة الشكل الفعلي — دون جلسة دفع أو حجز / Visual QA only</p>
<main class="checkout">{html}</main>
<script>document.getElementById("method-tab-apple-pay").hidden=false;
{tabs_source}</script>
</body></html>"""
    (args.output_dir / f"{language}.html").write_text(result, encoding="utf-8")
print("Synthetic checkout previews generated; no database or gateway accessed.")
