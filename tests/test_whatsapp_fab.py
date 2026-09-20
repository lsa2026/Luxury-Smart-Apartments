from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_whatsapp_fab_clears_mobile_booking_cta_and_consent_banner():
    javascript = (ROOT / "static/js/site.js").read_text(encoding="utf-8")
    stylesheet = (ROOT / "static/css/site.css").read_text(encoding="utf-8")

    assert 'document.querySelector("[data-mobile-booking-cta]")' in javascript
    assert 'document.querySelector("[data-consent-banner]")' in javascript
    assert "getBoundingClientRect" in javascript
    assert '"--whatsapp-fab-offset"' in javascript
    assert ".whatsapp-fab" in stylesheet
